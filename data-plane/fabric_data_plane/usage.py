"""Local usage buffering.

AR-DP01 gives the data plane local telemetry buffering. Records accumulate in a
bounded deque and the oldest are dropped rather than growing without limit or
failing a request. Dropped counts are reported so loss is visible instead of
silent.

The control plane now accepts usage, but the data plane does not send it: it never
holds the telemetry credential, which belongs to a collector-only Secret. A
collector drains this buffer through the administrative listener and forwards the
records, which keeps the inference path free of any export credential.

Each record carries a stable identifier assigned here, so a collector retrying a
forward is deduplicated centrally instead of double-counting.

Ownership on a record comes from the verified token and the local registry, never
from anything the client sent.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import threading
import uuid
from typing import Any


@dataclasses.dataclass(frozen=True)
class UsageRecord:
    """One completed inference call."""

    account_id: uuid.UUID
    deployment_id: uuid.UUID
    input_tokens: int
    output_tokens: int
    streamed: bool
    occurred_at: dt.datetime
    #: Assigned at record time, not at export time, so the identity of a record
    #: survives a collector restart and a retried forward stays idempotent.
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


class UsageBuffer:
    """Thread-safe bounded buffer of usage records."""

    def __init__(self, capacity: int) -> None:
        self._records: collections.deque[UsageRecord] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._recorded = 0
        self._dropped = 0

    def record(self, record: UsageRecord) -> None:
        with self._lock:
            if len(self._records) == self._records.maxlen:
                self._dropped += 1
            self._records.append(record)
            self._recorded += 1

    def drain(self) -> list[UsageRecord]:
        """Remove and return buffered records for a collector to forward."""
        with self._lock:
            drained = list(self._records)
            self._records.clear()
            return drained

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "buffered": len(self._records),
                "capacity": self._records.maxlen,
                "recorded": self._recorded,
                "dropped": self._dropped,
                # The data plane never pushes: records leave only when a collector
                # drains them, so a growing buffer means no collector is running.
                "export_mode": "drain",
            }
