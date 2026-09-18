"""Durable local usage spooling with an acknowledged collector handoff.

The data plane never holds a telemetry credential. It records completed inference usage into a
bounded local SQLite spool, and the collector reaches that spool only through the localhost
administrative listener. Records are leased non-destructively, forwarded, and deleted only after
the collector acknowledges a resolved control-plane response.

That order closes both volatile gaps in the previous design: a data-plane restart no longer loses
records waiting to be drained, and a collector restart after reading a batch no longer loses the
batch. A resend after a lost acknowledgement is safe because every record receives its stable
``record_id`` here and the control plane deduplicates that identity within the stamp.

The spool stays bounded. On overflow it drops the oldest record that is not in the outstanding
lease. Leased records are protected because the control plane may already be processing them; if
every retained record is leased, the new record is dropped instead. Both cases increment the
persistent drop counter so loss is visible.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import os
import pathlib
import sqlite3
import threading
import uuid
from typing import Any

DEFAULT_LEASE_SIZE = 500


class UsageSpoolUnavailable(RuntimeError):
    """The durable spool cannot currently accept trustworthy accounting writes."""


class UsageLeaseMismatch(ValueError):
    """An acknowledgement does not describe the complete outstanding lease."""


@dataclasses.dataclass(frozen=True)
class UsageRecord:
    """One completed inference call."""

    account_id: uuid.UUID
    deployment_id: uuid.UUID
    input_tokens: int
    output_tokens: int
    streamed: bool
    occurred_at: dt.datetime
    #: Assigned at record time, not at export time, so retries remain idempotent.
    record_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": str(self.record_id),
            "account_id": str(self.account_id),
            "deployment_id": str(self.deployment_id),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "streamed": self.streamed,
            "occurred_at": self.occurred_at.isoformat().replace("+00:00", "Z"),
        }


@dataclasses.dataclass(frozen=True)
class UsageLease:
    """One stable batch that remains in the spool until acknowledged."""

    lease_id: uuid.UUID | None
    records: list[UsageRecord]


@dataclasses.dataclass(frozen=True)
class UsageAcknowledgement:
    """Logical acknowledgement plus whether rows were deleted by this exact call."""

    acknowledged: int
    deleted: int
    already_acknowledged: bool


class UsageBuffer:
    """Thread-safe bounded usage spool.

    ``path=None`` uses an in-memory SQLite database for local development and unit tests. A file
    path enables restart durability; the Helm chart supplies one on a dedicated retained PVC.
    The constructor keeps ``capacity`` as its first keyword-compatible parameter for callers that
    predate the durable implementation.
    """

    def __init__(self, capacity: int, path: str | None = None) -> None:
        if capacity < 1:
            raise ValueError("usage spool capacity must be at least one")
        self._capacity = capacity
        self._path = pathlib.Path(path) if path else None
        self._lock = threading.Lock()
        self._healthy = True
        self._last_error: str | None = None

        location = ":memory:"
        if self._path is not None:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._path.parent.chmod(0o700)
            location = str(self._path)

        self._database = sqlite3.connect(
            location,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._database.row_factory = sqlite3.Row
        self._database.execute("PRAGMA busy_timeout = 5000")
        self._database.execute("PRAGMA synchronous = FULL")
        self._database.execute("PRAGMA journal_mode = WAL")
        self._initialize()
        self._reconcile_capacity()
        self._secure_files()

        if self._path is not None:
            self._sync_parent(self._path.parent)

    @staticmethod
    def _sync_parent(parent: pathlib.Path) -> None:
        """Persist the initial directory entry on filesystems that support directory fsync."""
        descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _initialize(self) -> None:
        self._database.executescript(
            """
            CREATE TABLE IF NOT EXISTS usage_records (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT NOT NULL UNIQUE,
                account_id TEXT NOT NULL,
                deployment_id TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                streamed INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                lease_id TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_usage_records_lease_sequence
                ON usage_records (lease_id, sequence);
            CREATE TABLE IF NOT EXISTS usage_metadata (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO usage_metadata (key, value) VALUES ('recorded', 0);
            INSERT OR IGNORE INTO usage_metadata (key, value) VALUES ('dropped', 0);
            CREATE TABLE IF NOT EXISTS usage_ack_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                lease_id TEXT,
                record_count INTEGER
            );
            INSERT OR IGNORE INTO usage_ack_state (
                singleton, lease_id, record_count
            ) VALUES (1, NULL, NULL);
            """
        )

    def _secure_files(self) -> None:
        if self._path is None:
            return
        for suffix in ("", "-wal", "-shm"):
            candidate = pathlib.Path(f"{self._path}{suffix}")
            if candidate.exists():
                candidate.chmod(0o600)

    def _mark_failed(self, exc: BaseException) -> None:
        self._healthy = False
        self._last_error = f"{type(exc).__name__}: {exc}"

    def _rollback_safely(self) -> None:
        try:
            self._database.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    @contextlib.contextmanager
    def _transaction(self):
        try:
            self._database.execute("BEGIN IMMEDIATE")
            yield
            self._database.execute("COMMIT")
            self._secure_files()
        except (sqlite3.Error, OSError) as exc:
            self._rollback_safely()
            self._mark_failed(exc)
            raise UsageSpoolUnavailable("durable usage spool is unavailable") from exc
        except BaseException:
            self._rollback_safely()
            raise

    def _reconcile_capacity(self) -> None:
        """Apply a lowered capacity to unleased rows while preserving any stable lease."""
        with self._transaction():
            total = int(
                self._database.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0]
            )
            excess = max(0, total - self._capacity)
            if not excess:
                return
            removable = self._database.execute(
                """
                SELECT sequence FROM usage_records
                WHERE lease_id IS NULL
                ORDER BY sequence
                LIMIT ?
                """,
                (excess,),
            ).fetchall()
            if removable:
                self._database.executemany(
                    "DELETE FROM usage_records WHERE sequence = ?",
                    [(row["sequence"],) for row in removable],
                )
                self._increment("dropped", len(removable))

    @property
    def healthy(self) -> bool:
        return self._healthy

    def _increment(self, key: str, amount: int = 1) -> None:
        self._database.execute(
            "UPDATE usage_metadata SET value = value + ? WHERE key = ?",
            (amount, key),
        )

    def record(self, record: UsageRecord) -> None:
        """Persist one completed request before returning to response cleanup."""
        with self._lock, self._transaction():
            if self._database.execute(
                "SELECT 1 FROM usage_records WHERE record_id = ?",
                (str(record.record_id),),
            ).fetchone() is not None:
                return
            total = int(
                self._database.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0]
            )
            needed = max(0, total - self._capacity + 1)
            if needed:
                removable = self._database.execute(
                    """
                    SELECT sequence FROM usage_records
                    WHERE lease_id IS NULL
                    ORDER BY sequence
                    LIMIT ?
                    """,
                    (needed,),
                ).fetchall()
                if len(removable) < needed:
                    # Every remaining slot is protected by the outstanding lease. Preserve
                    # those records and account for dropping this new one instead.
                    self._increment("recorded")
                    self._increment("dropped")
                    return
                self._database.executemany(
                    "DELETE FROM usage_records WHERE sequence = ?",
                    [(row["sequence"],) for row in removable],
                )
                self._increment("dropped", len(removable))

            values = record.as_dict()
            self._database.execute(
                """
                INSERT INTO usage_records (
                    record_id, account_id, deployment_id, input_tokens,
                    output_tokens, streamed, occurred_at, lease_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    values["record_id"],
                    values["account_id"],
                    values["deployment_id"],
                    values["input_tokens"],
                    values["output_tokens"],
                    int(values["streamed"]),
                    values["occurred_at"],
                ),
            )
            self._increment("recorded")

    def lease(self, limit: int = DEFAULT_LEASE_SIZE) -> UsageLease:
        """Return the outstanding lease, or create one from the oldest available records.

        There is exactly one outstanding lease. A collector restart, timeout, or lost response
        therefore receives the same batch again, with the same record IDs. The control plane's
        deduplication contract makes that at-least-once replay safe.
        """
        if limit < 1 or limit > DEFAULT_LEASE_SIZE:
            raise ValueError(f"usage lease limit must be between 1 and {DEFAULT_LEASE_SIZE}")
        with self._lock, self._transaction():
            existing = self._database.execute(
                """
                SELECT lease_id FROM usage_records
                WHERE lease_id IS NOT NULL
                ORDER BY sequence
                LIMIT 1
                """
            ).fetchone()
            if existing is not None:
                lease_id = uuid.UUID(existing["lease_id"])
            else:
                rows = self._database.execute(
                    """
                    SELECT sequence FROM usage_records
                    WHERE lease_id IS NULL
                    ORDER BY sequence
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                if not rows:
                    return UsageLease(lease_id=None, records=[])
                lease_id = uuid.uuid4()
                self._database.executemany(
                    "UPDATE usage_records SET lease_id = ? WHERE sequence = ?",
                    [(str(lease_id), row["sequence"]) for row in rows],
                )

            records = self._database.execute(
                """
                SELECT * FROM usage_records
                WHERE lease_id = ?
                ORDER BY sequence
                """,
                (str(lease_id),),
            ).fetchall()
            return UsageLease(
                lease_id=lease_id,
                records=[self._record_from_row(row) for row in records],
            )

    def acknowledge(
        self, lease_id: uuid.UUID, expected_count: int
    ) -> UsageAcknowledgement:
        """Resolve one complete lease; repeated matching acknowledgements are explicit."""
        if expected_count < 1 or expected_count > DEFAULT_LEASE_SIZE:
            raise UsageLeaseMismatch("expected_count is outside the lease bound")
        with self._lock, self._transaction():
            current = int(
                self._database.execute(
                    "SELECT COUNT(*) FROM usage_records WHERE lease_id = ?",
                    (str(lease_id),),
                ).fetchone()[0]
            )
            if current == 0:
                previous = self._database.execute(
                    "SELECT lease_id, record_count FROM usage_ack_state WHERE singleton = 1"
                ).fetchone()
                if (
                    previous is not None
                    and previous["lease_id"] == str(lease_id)
                    and int(previous["record_count"]) == expected_count
                ):
                    return UsageAcknowledgement(
                        acknowledged=expected_count,
                        deleted=0,
                        already_acknowledged=True,
                    )
                raise UsageLeaseMismatch("lease is not outstanding or was superseded")
            if current != expected_count:
                raise UsageLeaseMismatch(
                    f"lease contains {current} records, acknowledgement expected {expected_count}"
                )
            cursor = self._database.execute(
                "DELETE FROM usage_records WHERE lease_id = ?",
                (str(lease_id),),
            )
            self._database.execute(
                """
                UPDATE usage_ack_state
                SET lease_id = ?, record_count = ?
                WHERE singleton = 1
                """,
                (str(lease_id), expected_count),
            )
            return UsageAcknowledgement(
                acknowledged=expected_count,
                deleted=cursor.rowcount,
                already_acknowledged=False,
            )

    def drain(self) -> list[UsageRecord]:
        """Compatibility helper for local tests; production export uses lease/ack.

        This deliberately removes all records and must not be exposed as the collector protocol.
        """
        with self._lock, self._transaction():
            rows = self._database.execute(
                "SELECT * FROM usage_records ORDER BY sequence"
            ).fetchall()
            self._database.execute("DELETE FROM usage_records")
            return [self._record_from_row(row) for row in rows]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if not self._healthy:
                return {
                    "healthy": False,
                    "last_error": self._last_error,
                    "capacity": self._capacity,
                    "durable": self._path is not None,
                    "export_mode": "lease_ack",
                }
            try:
                total = int(
                    self._database.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0]
                )
                leased = int(
                    self._database.execute(
                        "SELECT COUNT(*) FROM usage_records WHERE lease_id IS NOT NULL"
                    ).fetchone()[0]
                )
                metadata = {
                    row["key"]: int(row["value"])
                    for row in self._database.execute(
                        "SELECT key, value FROM usage_metadata"
                    ).fetchall()
                }
            except (sqlite3.Error, OSError) as exc:
                self._mark_failed(exc)
                return {
                    "healthy": False,
                    "last_error": self._last_error,
                    "capacity": self._capacity,
                    "durable": self._path is not None,
                    "export_mode": "lease_ack",
                }
            return {
                "healthy": True,
                "last_error": None,
                "buffered": total,
                "leased": leased,
                "available": total - leased,
                "capacity": self._capacity,
                "recorded": metadata["recorded"],
                "dropped": metadata["dropped"],
                "durable": self._path is not None,
                "export_mode": "lease_ack",
            }

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> UsageRecord:
        occurred_at = dt.datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))
        return UsageRecord(
            record_id=uuid.UUID(row["record_id"]),
            account_id=uuid.UUID(row["account_id"]),
            deployment_id=uuid.UUID(row["deployment_id"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            streamed=bool(row["streamed"]),
            occurred_at=occurred_at,
        )

    def close(self) -> None:
        with self._lock:
            self._database.close()
