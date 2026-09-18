"""Stamp-local shared rate and concurrency coordination.

The inference path must keep working during a central control-plane outage, so shared limits are
coordinated inside the stamp rather than through the control plane or its database. Every gateway
replica calls one private coordinator. The coordinator persists token buckets, idempotent admission
decisions, and renewable concurrency leases in SQLite on the stamp's retained state volume.

A coordinator outage fails new requests closed: failing open would make the configured cap false.
Requests already admitted keep running; their leases are renewed while possible and expire after a
client crash. A failed release is conservative — it temporarily under-admits until lease expiry.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hmac
import logging
import math
import pathlib
import sqlite3
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from fabric_data_plane.limits import ConcurrencyLimiter, RateLimit, RateLimiter

logger = logging.getLogger("fabric.data_plane.limits")


class LimitCoordinatorUnavailable(RuntimeError):
    """The stamp-local coordinator cannot make a trustworthy admission decision."""


@dataclasses.dataclass(frozen=True)
class Admission:
    """One combined rate/concurrency decision."""

    allowed: bool
    reason: str | None = None
    retry_after: float | None = None
    lease_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None


class LocalLimitManager:
    """Current one-process semantics behind the same interface as shared coordination."""

    mode = "process"

    def __init__(self, rate: RateLimiter, concurrency: ConcurrencyLimiter) -> None:
        self.rate = rate
        self.concurrency = concurrency

    async def acquire(self, account_id: uuid.UUID) -> Admission:
        wait = self.rate.check(account_id)
        if wait is not None:
            return Admission(False, reason="rate_limited", retry_after=wait)
        if not await self.concurrency.acquire(account_id):
            return Admission(False, reason="too_many_in_flight", retry_after=1.0)
        return Admission(True, account_id=account_id)

    async def release(self, admission: Admission) -> None:
        if admission.allowed and admission.account_id is not None:
            await self.concurrency.release(admission.account_id)

    async def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "healthy": True,
            "rate_limit": self.rate.snapshot(),
            "concurrency": self.concurrency.snapshot(),
        }


class LimitCoordinatorStore:
    """SQLite authority shared by every gateway through one private HTTP service."""

    def __init__(
        self,
        *,
        path: str,
        rate: RateLimit,
        maximum: int,
        lease_seconds: float,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("limit lease duration must be positive")
        self.rate = rate
        self.maximum = maximum
        self.lease_seconds = lease_seconds
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._bucket_updated: dict[str, float] = {}
        self._lease_deadlines: dict[str, float] = {}
        self._admission_deadlines: dict[str, float] = {}
        self._path = pathlib.Path(path)
        self._path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._path.parent.chmod(0o700)
        self._database = sqlite3.connect(
            self._path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._database.row_factory = sqlite3.Row
        self._database.execute("PRAGMA busy_timeout = 5000")
        self._database.execute("PRAGMA synchronous = FULL")
        # A coordinator owns this file and all clients use HTTP, so WAL gives crash recovery
        # without relying on network-filesystem locking between gateway replicas.
        self._database.execute("PRAGMA journal_mode = WAL")
        self._lock = asyncio.Lock()
        self._healthy = True
        self._last_error: str | None = None
        self._initialize()
        self._reconcile_restart_state()
        self._secure_files()

    @property
    def enabled(self) -> bool:
        return self.rate.enabled or self.maximum > 0

    @property
    def healthy(self) -> bool:
        return self._healthy

    def _initialize(self) -> None:
        self._database.executescript(
            """
            CREATE TABLE IF NOT EXISTS rate_buckets (
                account_id TEXT PRIMARY KEY,
                tokens REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS concurrency_leases (
                lease_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_limit_leases_account_expiry
                ON concurrency_leases (account_id, expires_at);
            CREATE TABLE IF NOT EXISTS admissions (
                request_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                allowed INTEGER NOT NULL,
                reason TEXT,
                retry_after REAL,
                lease_id TEXT,
                expires_at REAL NOT NULL,
                released INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS limit_metadata (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO limit_metadata (key, value) VALUES ('allowed', 0);
            INSERT OR IGNORE INTO limit_metadata (key, value) VALUES ('rate_allowed', 0);
            INSERT OR IGNORE INTO limit_metadata (key, value) VALUES ('concurrency_allowed', 0);
            INSERT OR IGNORE INTO limit_metadata (key, value) VALUES ('rate_rejected', 0);
            INSERT OR IGNORE INTO limit_metadata (key, value) VALUES ('concurrency_rejected', 0);
            INSERT OR IGNORE INTO limit_metadata (key, value) VALUES ('expired_leases', 0);
            CREATE TABLE IF NOT EXISTS limit_policy (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                requests_per_minute INTEGER NOT NULL,
                burst INTEGER NOT NULL,
                maximum INTEGER NOT NULL
            );
            """
        )

    def _reconcile_restart_state(self) -> None:
        """Resume conservatively without trusting wall-clock downtime.

        Persisted tokens are never refilled merely because the process restarted or wall time
        jumped. Existing leases receive one full lease interval from this process start, avoiding
        oversubscription if a gateway request survived a coordinator restart. Policy changes reset
        buckets at an explicit now-boundary instead of applying a new refill rate retroactively.
        """
        wall = self._wall_clock()
        monotonic = self._monotonic_clock()
        self._database.execute("BEGIN IMMEDIATE")
        try:
            previous = self._database.execute(
                "SELECT requests_per_minute, burst, maximum FROM limit_policy WHERE singleton = 1"
            ).fetchone()
            policy = (self.rate.requests_per_minute, self.rate.burst, self.maximum)
            rate_changed = previous is not None and (
                int(previous["requests_per_minute"]), int(previous["burst"])
            ) != policy[:2]
            policy_changed = previous is not None and tuple(previous) != policy
            if previous is None:
                self._database.execute(
                    """
                    INSERT INTO limit_policy (
                        singleton, requests_per_minute, burst, maximum
                    ) VALUES (1, ?, ?, ?)
                    """,
                    policy,
                )
            elif policy_changed:
                self._database.execute(
                    """
                    UPDATE limit_policy
                    SET requests_per_minute = ?, burst = ?, maximum = ?
                    WHERE singleton = 1
                    """,
                    policy,
                )
                if rate_changed:
                    self._database.execute(
                        "UPDATE rate_buckets SET tokens = ?, updated_at = ?",
                        (float(self.rate.burst), wall),
                    )
                else:
                    # A concurrency-only change has no bearing on rate history.
                    self._database.execute(
                        "UPDATE rate_buckets SET updated_at = ?",
                        (wall,),
                    )
            else:
                # Preserve tokens but establish a fresh monotonic origin. Downtime grants no
                # unprovable refill; normal refill resumes from process start.
                self._database.execute(
                    "UPDATE rate_buckets SET updated_at = ?",
                    (wall,),
                )

            bucket_accounts = self._database.execute(
                "SELECT account_id FROM rate_buckets"
            ).fetchall()
            self._bucket_updated = {row["account_id"]: monotonic for row in bucket_accounts}

            leases = self._database.execute(
                "SELECT lease_id FROM concurrency_leases"
            ).fetchall()
            self._lease_deadlines = {
                row["lease_id"]: monotonic + self.lease_seconds for row in leases
            }
            self._database.execute(
                "UPDATE concurrency_leases SET expires_at = ?",
                (wall + self.lease_seconds,),
            )

            admissions = self._database.execute(
                "SELECT request_id FROM admissions"
            ).fetchall()
            self._admission_deadlines = {
                row["request_id"]: monotonic + self.lease_seconds for row in admissions
            }
            self._database.execute(
                "UPDATE admissions SET expires_at = ?",
                (wall + self.lease_seconds,),
            )
            self._database.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                self._database.execute("ROLLBACK")
            raise

    def _secure_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = pathlib.Path(f"{self._path}{suffix}")
            if candidate.exists():
                candidate.chmod(0o600)

    @contextlib.contextmanager
    def _transaction(self):
        try:
            self._database.execute("BEGIN IMMEDIATE")
            yield
            self._database.execute("COMMIT")
            self._secure_files()
        except (sqlite3.Error, OSError) as exc:
            with contextlib.suppress(sqlite3.Error):
                self._database.execute("ROLLBACK")
            self._healthy = False
            self._last_error = f"{type(exc).__name__}: {exc}"
            raise LimitCoordinatorUnavailable("shared limit store is unavailable") from exc
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                self._database.execute("ROLLBACK")
            raise

    def _increment(self, key: str, amount: int = 1) -> None:
        self._database.execute(
            "UPDATE limit_metadata SET value = value + ? WHERE key = ?",
            (amount, key),
        )

    def _purge(self, monotonic: float) -> None:
        expired_leases = [
            lease_id
            for lease_id, deadline in self._lease_deadlines.items()
            if deadline <= monotonic
        ]
        if expired_leases:
            self._database.executemany(
                "DELETE FROM concurrency_leases WHERE lease_id = ?",
                [(lease_id,) for lease_id in expired_leases],
            )
            for lease_id in expired_leases:
                del self._lease_deadlines[lease_id]
            self._increment("expired_leases", len(expired_leases))

        expired_admissions = [
            request_id
            for request_id, deadline in self._admission_deadlines.items()
            if deadline <= monotonic
        ]
        if expired_admissions:
            self._database.executemany(
                "DELETE FROM admissions WHERE request_id = ?",
                [(request_id,) for request_id in expired_admissions],
            )
            for request_id in expired_admissions:
                del self._admission_deadlines[request_id]

    @staticmethod
    def _admission(row: sqlite3.Row) -> Admission:
        return Admission(
            allowed=bool(row["allowed"]),
            reason=row["reason"],
            retry_after=row["retry_after"],
            lease_id=uuid.UUID(row["lease_id"]) if row["lease_id"] else None,
            account_id=uuid.UUID(row["account_id"]),
        )

    async def acquire(
        self,
        account_id: uuid.UUID,
        request_id: uuid.UUID,
    ) -> Admission:
        monotonic = self._monotonic_clock()
        wall = self._wall_clock()
        async with self._lock:
            with self._transaction():
                self._purge(monotonic)
                existing = self._database.execute(
                    "SELECT * FROM admissions WHERE request_id = ?",
                    (str(request_id),),
                ).fetchone()
                if existing is not None:
                    if existing["account_id"] != str(account_id) or existing["released"]:
                        raise ValueError(
                            "request_id was already used by another or released request"
                        )
                    return self._admission(existing)

                wait: float | None = None
                tokens = float(self.rate.burst)
                if self.rate.enabled:
                    bucket = self._database.execute(
                        "SELECT tokens, updated_at FROM rate_buckets WHERE account_id = ?",
                        (str(account_id),),
                    ).fetchone()
                    if bucket is not None:
                        updated = self._bucket_updated.get(str(account_id), monotonic)
                        elapsed = max(0.0, monotonic - updated)
                        tokens = min(
                            float(self.rate.burst),
                            float(bucket["tokens"]) + elapsed * self.rate.refill_per_second,
                        )
                    if tokens >= 1.0:
                        tokens -= 1.0
                    else:
                        wait = (1.0 - tokens) / self.rate.refill_per_second
                    self._database.execute(
                        """
                        INSERT INTO rate_buckets (account_id, tokens, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(account_id) DO UPDATE SET
                            tokens = excluded.tokens,
                            updated_at = excluded.updated_at
                        """,
                        (str(account_id), tokens, wall),
                    )
                    self._bucket_updated[str(account_id)] = monotonic

                reason: str | None = None
                lease_id: uuid.UUID | None = None
                allowed = wait is None
                if wait is not None:
                    reason = "rate_limited"
                    self._increment("rate_rejected")
                else:
                    if self.rate.enabled:
                        self._increment("rate_allowed")
                if wait is None and self.maximum > 0:
                    current = int(
                        self._database.execute(
                            """
                            SELECT COUNT(*) FROM concurrency_leases
                            WHERE account_id = ?
                            """,
                            (str(account_id),),
                        ).fetchone()[0]
                    )
                    if current >= self.maximum:
                        allowed = False
                        reason = "too_many_in_flight"
                        wait = 1.0
                        self._increment("concurrency_rejected")
                    else:
                        lease_id = request_id
                        self._database.execute(
                            """
                            INSERT INTO concurrency_leases (lease_id, account_id, expires_at)
                            VALUES (?, ?, ?)
                            """,
                            (str(lease_id), str(account_id), wall + self.lease_seconds),
                        )
                        self._lease_deadlines[str(lease_id)] = (
                            monotonic + self.lease_seconds
                        )
                        self._increment("concurrency_allowed")

                if allowed:
                    self._increment("allowed")
                decision_ttl = self.lease_seconds if lease_id else max(60.0, wait or 0.0)
                self._database.execute(
                    """
                    INSERT INTO admissions (
                        request_id, account_id, allowed, reason, retry_after,
                        lease_id, expires_at, released
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        str(request_id),
                        str(account_id),
                        int(allowed),
                        reason,
                        wait,
                        str(lease_id) if lease_id else None,
                        wall + decision_ttl,
                    ),
                )
                self._admission_deadlines[str(request_id)] = monotonic + decision_ttl
                return Admission(
                    allowed,
                    reason=reason,
                    retry_after=wait,
                    lease_id=lease_id,
                    account_id=account_id,
                )

    async def renew(self, lease_id: uuid.UUID) -> bool:
        monotonic = self._monotonic_clock()
        wall = self._wall_clock()
        async with self._lock:
            with self._transaction():
                self._purge(monotonic)
                updated = self._database.execute(
                    "UPDATE concurrency_leases SET expires_at = ? WHERE lease_id = ?",
                    (wall + self.lease_seconds, str(lease_id)),
                ).rowcount
                if updated:
                    self._lease_deadlines[str(lease_id)] = monotonic + self.lease_seconds
                    self._admission_deadlines[str(lease_id)] = monotonic + self.lease_seconds
                    self._database.execute(
                        "UPDATE admissions SET expires_at = ? WHERE request_id = ?",
                        (wall + self.lease_seconds, str(lease_id)),
                    )
                return bool(updated)

    async def release(self, lease_id: uuid.UUID) -> bool:
        async with self._lock:
            with self._transaction():
                deleted = self._database.execute(
                    "DELETE FROM concurrency_leases WHERE lease_id = ?",
                    (str(lease_id),),
                ).rowcount
                self._lease_deadlines.pop(str(lease_id), None)
                self._database.execute(
                    "UPDATE admissions SET released = 1 WHERE request_id = ?",
                    (str(lease_id),),
                )
                return bool(deleted)

    async def snapshot(self) -> dict[str, Any]:
        monotonic = self._monotonic_clock()
        async with self._lock:
            with self._transaction():
                self._purge(monotonic)
                in_flight = int(
                    self._database.execute("SELECT COUNT(*) FROM concurrency_leases").fetchone()[0]
                )
                accounts = int(
                    self._database.execute(
                        "SELECT COUNT(DISTINCT account_id) FROM concurrency_leases"
                    ).fetchone()[0]
                )
                metadata = {
                    row["key"]: int(row["value"])
                    for row in self._database.execute(
                        "SELECT key, value FROM limit_metadata"
                    ).fetchall()
                }
                return {
                    "mode": "coordinator",
                    "healthy": self._healthy,
                    "last_error": self._last_error,
                    "admitted": metadata["allowed"],
                    "rate_limit": {
                        "enabled": self.rate.enabled,
                        "requests_per_minute": self.rate.requests_per_minute,
                        "burst": self.rate.burst,
                        "accounts_tracked": int(
                            self._database.execute(
                                "SELECT COUNT(*) FROM rate_buckets"
                            ).fetchone()[0]
                        ),
                        "allowed": metadata["rate_allowed"],
                        "rejected": metadata["rate_rejected"],
                    },
                    "concurrency": {
                        "enabled": self.maximum > 0,
                        "max_in_flight_per_account": self.maximum,
                        "in_flight": in_flight,
                        "accounts_in_flight": accounts,
                        "rejected": metadata["concurrency_rejected"],
                        "allowed": metadata["concurrency_allowed"],
                        "expired_leases": metadata["expired_leases"],
                        "lease_seconds": self.lease_seconds,
                    },
                }

    def close(self) -> None:
        self._database.close()


class SharedLimitManager:
    """Gateway-side client for the private stamp-local coordinator."""

    mode = "shared"

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float,
        renew_seconds: float,
        rate_enabled: bool,
        concurrency_enabled: bool,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token:
            raise ValueError("shared limit coordinator token is required")
        if renew_seconds <= 0:
            raise ValueError("limit lease renewal interval must be positive")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.renew_seconds = renew_seconds
        self.rate_enabled = rate_enabled
        self.concurrency_enabled = concurrency_enabled
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._renewals: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._healthy = True
        self._last_error: str | None = None
        self._protocol_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.rate_enabled or self.concurrency_enabled

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _mark_error(self, exc: BaseException) -> LimitCoordinatorUnavailable:
        self._healthy = False
        self._last_error = f"{type(exc).__name__}: {exc}"
        return LimitCoordinatorUnavailable("stamp limit coordinator is unavailable")

    def _mark_protocol_error(self, exc: BaseException) -> LimitCoordinatorUnavailable:
        self._protocol_error = f"{type(exc).__name__}: {exc}"
        self._healthy = False
        self._last_error = self._protocol_error
        return LimitCoordinatorUnavailable("stamp limit coordinator protocol is invalid")

    def _mark_healthy(self, *, admission_validated: bool = False) -> None:
        # Only `/limits/admit` proves the endpoint every new request depends on is compatible.
        # Renewal/release/state success may restore transport health but cannot erase an admission
        # schema error while new work would still fail.
        if admission_validated:
            self._protocol_error = None
        if self._protocol_error is not None:
            self._healthy = False
            self._last_error = self._protocol_error
            return
        self._healthy = True
        self._last_error = None

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        attempts: int = 1,
        status_is_protocol: bool = False,
    ) -> dict[str, Any]:
        last_error: BaseException | None = None
        for attempt in range(attempts):
            try:
                response = await self._client.post(
                    f"{self.base_url}{path}",
                    json=payload,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                if response.status_code >= 500 and attempt + 1 < attempts:
                    last_error = httpx.HTTPStatusError(
                        "coordinator server error",
                        request=response.request,
                        response=response,
                    )
                    continue
                response.raise_for_status()
                document = response.json()
                if not isinstance(document, dict):
                    raise ValueError("coordinator response is not an object")
                return document
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    continue
                raise self._mark_error(exc) from exc
            except ValueError as exc:
                raise self._mark_protocol_error(exc) from exc
            except httpx.HTTPStatusError as exc:
                if status_is_protocol:
                    raise self._mark_protocol_error(exc) from exc
                raise self._mark_error(exc) from exc
            except httpx.HTTPError as exc:
                raise self._mark_error(exc) from exc
        assert last_error is not None
        raise self._mark_error(last_error) from last_error

    async def acquire(self, account_id: uuid.UUID) -> Admission:
        if not self.enabled:
            return Admission(True, account_id=account_id)
        request_id = uuid.uuid4()
        document = await self._post(
            "/limits/admit",
            {"account_id": str(account_id), "request_id": str(request_id)},
            attempts=2,
            status_is_protocol=True,
        )
        try:
            allowed = document.get("allowed")
            if not isinstance(allowed, bool):
                raise ValueError("coordinator admission omitted allowed")
            reason = document.get("reason")
            retry_after = document.get("retry_after")
            raw_lease = document.get("lease_id")
            if raw_lease is not None and not isinstance(raw_lease, str):
                raise ValueError("coordinator returned a non-string lease")
            lease_id = uuid.UUID(raw_lease) if isinstance(raw_lease, str) else None
            if allowed:
                if reason is not None or retry_after is not None:
                    raise ValueError("allowed coordinator admission carries refusal fields")
                if self.concurrency_enabled and lease_id != request_id:
                    raise ValueError("coordinator lease is not bound to this request")
                if not self.concurrency_enabled and lease_id is not None:
                    raise ValueError("coordinator issued a lease while concurrency is disabled")
            else:
                if lease_id is not None:
                    raise ValueError("refused coordinator admission carries a lease")
                if reason not in {"rate_limited", "too_many_in_flight"}:
                    raise ValueError("coordinator returned an unknown refusal")
                if (
                    isinstance(retry_after, bool)
                    or not isinstance(retry_after, int | float)
                    or not math.isfinite(float(retry_after))
                    or retry_after <= 0
                ):
                    raise ValueError("coordinator refusal omitted a valid retry_after")
        except (TypeError, ValueError) as exc:
            raise self._mark_protocol_error(exc) from exc

        self._mark_healthy(admission_validated=True)
        admission = Admission(
            allowed,
            reason=reason if isinstance(reason, str) else None,
            retry_after=float(retry_after) if isinstance(retry_after, int | float) else None,
            lease_id=lease_id,
            account_id=account_id,
        )
        if lease_id is not None:
            self._renewals[lease_id] = asyncio.create_task(self._renew(lease_id))
        return admission

    async def _renew(self, lease_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(self.renew_seconds)
            try:
                document = await self._post(
                    "/limits/renew",
                    {"lease_id": str(lease_id)},
                )
                if document.get("lease_id") != str(lease_id) or not isinstance(
                    document.get("renewed"), bool
                ):
                    raise self._mark_protocol_error(
                        ValueError("malformed lease renewal response")
                    )
                if document["renewed"] is not True:
                    self._healthy = False
                    self._last_error = "coordinator no longer recognizes an active lease"
                    logger.error("shared concurrency lease %s expired before renewal", lease_id)
                    return
                self._mark_healthy()
            except LimitCoordinatorUnavailable as exc:
                # Existing work continues, but new admissions fail closed while health is false.
                logger.error("could not renew shared concurrency lease %s: %s", lease_id, exc)

    async def release(self, admission: Admission) -> None:
        lease_id = admission.lease_id
        if lease_id is None:
            return
        renewal = self._renewals.pop(lease_id, None)
        if renewal is not None:
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewal
        try:
            document = await self._post(
                "/limits/release", {"lease_id": str(lease_id)}
            )
            if document.get("lease_id") != str(lease_id) or not isinstance(
                document.get("released"), bool
            ):
                raise self._mark_protocol_error(
                    ValueError("malformed lease release response")
                )
            self._mark_healthy()
        except LimitCoordinatorUnavailable as exc:
            # Conservative: the coordinator retains the slot until its bounded expiry.
            logger.error("could not release shared concurrency lease %s: %s", lease_id, exc)

    async def snapshot(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "mode": self.mode,
                "healthy": True,
                "rate_limit": {"enabled": False},
                "concurrency": {"enabled": False},
            }
        try:
            response = await self._client.get(
                f"{self.base_url}/limits/state",
                headers=self._headers(),
                timeout=self.timeout,
            )
            response.raise_for_status()
            document = response.json()
            if not isinstance(document, dict):
                raise ValueError("coordinator state is not an object")
            if document.get("mode") != "coordinator":
                raise ValueError("coordinator state has the wrong mode")
            if not isinstance(document.get("healthy"), bool):
                raise ValueError("coordinator state omitted healthy")
            if not isinstance(document.get("rate_limit"), dict) or not isinstance(
                document.get("concurrency"), dict
            ):
                raise ValueError("coordinator state omitted limit sections")
            coordinator_healthy = bool(document["healthy"])
            if self._protocol_error is not None:
                self._healthy = False
                self._last_error = self._protocol_error
                document = dict(document)
                document["healthy"] = False
                document["last_error"] = self._protocol_error
            else:
                self._healthy = coordinator_healthy
                self._last_error = (
                    None if self._healthy else str(document.get("last_error") or "unhealthy")
                )
            document["client_healthy"] = True
            return document
        except ValueError as exc:
            self._protocol_error = f"{type(exc).__name__}: {exc}"
            self._healthy = False
            self._last_error = self._protocol_error
            return {
                "mode": self.mode,
                "healthy": False,
                "last_error": self._last_error,
            }
        except httpx.HTTPError as exc:
            self._healthy = False
            self._last_error = f"{type(exc).__name__}: {exc}"
            return {
                "mode": self.mode,
                "healthy": False,
                "last_error": self._last_error,
            }

    async def close(self) -> None:
        for task in self._renewals.values():
            task.cancel()
        if self._renewals:
            await asyncio.gather(*self._renewals.values(), return_exceptions=True)
        self._renewals.clear()
        if self._owns_client:
            await self._client.aclose()


def authorized(authorization: str | None, token: str) -> bool:
    """Constant-time validation for the private coordinator credential."""
    if not authorization or not authorization.startswith("Bearer "):
        return False
    return hmac.compare_digest(authorization[len("Bearer ") :], token)
