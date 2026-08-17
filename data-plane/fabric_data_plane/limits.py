"""Rate limiting and concurrency capping.

A stamp serves a fixed amount of GPU. Without limits one account can consume all of it,
and the model host's own queue is the only thing pushing back, which it does by making
every request slow rather than by refusing any. Refusing early is better for everyone:
the caller learns immediately and the accounts behind it keep their share.

Two mechanisms, because they protect against different things:

* A rate limit bounds how often an account may ask. It smooths bursts and is the one a
  caller can plan around, so it is expressed in requests per minute with an explicit
  burst.
* A concurrency cap bounds how many of an account's requests may be in flight. This is
  the one that actually protects the GPU, because a model server's cost is set by how
  many sequences it is decoding at once, not by how many arrived per minute.

Both are per account rather than per deployment. The scarce resource is the device, and
an account with several deployments on one stamp shares it either way.

State is per process and deliberately not shared. A stamp runs one data plane per pod,
so a cluster-wide limit would need coordination that buys little: the pods behind one
Service each hold a fraction of the limit, and the model host behind them is the real
bound. This is documented rather than hidden, since it means a limit is approximate when
replicas are scaled.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid
from typing import Any


@dataclasses.dataclass
class RateLimit:
    """How often an account may ask, and how much it may burst."""

    requests_per_minute: int
    burst: int

    @property
    def enabled(self) -> bool:
        return self.requests_per_minute > 0

    @property
    def refill_per_second(self) -> float:
        return self.requests_per_minute / 60.0


class _Bucket:
    """A token bucket, which is what makes a burst allowance expressible.

    A fixed window would let a caller send its whole allowance in the last instant of one
    window and again in the first instant of the next, producing twice the intended rate
    at the boundary. A bucket refills continuously, so the limit holds everywhere.
    """

    __slots__ = ("tokens", "updated_at")

    def __init__(self, tokens: float, now: float) -> None:
        self.tokens = tokens
        self.updated_at = now

    def take(self, limit: RateLimit, now: float) -> tuple[bool, float]:
        """Consume one token, returning whether it was available and the wait if not."""
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(float(limit.burst), self.tokens + elapsed * limit.refill_per_second)
        self.updated_at = now

        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0.0

        # How long until one token exists, so the caller can be told when to return
        # instead of guessing.
        missing = 1.0 - self.tokens
        return False, missing / limit.refill_per_second


class RateLimiter:
    """Per-account token buckets."""

    def __init__(self, limit: RateLimit) -> None:
        self._limit = limit
        self._buckets: dict[uuid.UUID, _Bucket] = {}
        self._rejected = 0
        self._allowed = 0

    @property
    def enabled(self) -> bool:
        return self._limit.enabled

    def check(self, account_id: uuid.UUID, *, now: float | None = None) -> float | None:
        """Return ``None`` when allowed, or the seconds to wait when not."""
        if not self._limit.enabled:
            return None

        moment = now if now is not None else time.monotonic()
        bucket = self._buckets.get(account_id)
        if bucket is None:
            # A new account starts with a full burst rather than empty, so its first
            # request is not penalised for having no history.
            bucket = _Bucket(tokens=float(self._limit.burst), now=moment)
            self._buckets[account_id] = bucket

        allowed, wait = bucket.take(self._limit, moment)
        if allowed:
            self._allowed += 1
            return None
        self._rejected += 1
        return wait

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self._limit.enabled,
            "requests_per_minute": self._limit.requests_per_minute,
            "burst": self._limit.burst,
            "accounts_tracked": len(self._buckets),
            "allowed": self._allowed,
            "rejected": self._rejected,
        }


class ConcurrencyLimiter:
    """Caps how many of an account's requests may be in flight at once."""

    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self._in_flight: dict[uuid.UUID, int] = {}
        self._rejected = 0
        self._peak = 0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._maximum > 0

    async def acquire(self, account_id: uuid.UUID) -> bool:
        if not self.enabled:
            return True
        async with self._lock:
            current = self._in_flight.get(account_id, 0)
            if current >= self._maximum:
                self._rejected += 1
                return False
            self._in_flight[account_id] = current + 1
            self._peak = max(self._peak, current + 1)
            return True

    async def release(self, account_id: uuid.UUID) -> None:
        if not self.enabled:
            return
        async with self._lock:
            current = self._in_flight.get(account_id, 0)
            if current <= 1:
                # Removed rather than left at zero, so an account that stops sending
                # does not occupy memory for the lifetime of the process.
                self._in_flight.pop(account_id, None)
                return
            self._in_flight[account_id] = current - 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_in_flight_per_account": self._maximum,
            "in_flight": sum(self._in_flight.values()),
            "peak_per_account": self._peak,
            "rejected": self._rejected,
        }
