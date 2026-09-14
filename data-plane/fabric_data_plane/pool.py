"""Per-deployment pool of model-host backends and their health.

A deployment used to be one model host reached through one ``upstream_url``. A fleet
has several, so a deployment now carries a *pool* of backends and the data plane picks
one per request (ADR 0010). The operator publishes the concrete addresses into the
configuration document; the data plane never resolves them per request, so a lookup
never blocks the request path (AR-ID04).

Health is tracked per backend and per data-plane process. A backend that fails to answer
is ejected for a cooling interval so requests stop being sent to it, and it is tried
again once the interval passes rather than being written off for the life of the
process: a model host that restarted should be used again without the data plane
restarting too. Health being per-process is the same fleet-level approximation the limits
made before ADR 0009 (each replica balances over its own view); it is corrected where it
matters, not a correctness bug for a single-pod stamp.

Selection here is a single healthy pick, round-robin over the healthy backends. The
pluggable balancing *strategies* (least-in-flight, weighted) are a later decision (M2):
``BackendPool.select`` is the one place that changes, so a strategy can replace it
without touching the health tracker or the pool shape.
"""

from __future__ import annotations

import dataclasses
import itertools
import threading
import time


@dataclasses.dataclass(frozen=True)
class Backend:
    """One model-host endpoint a deployment can be served from.

    ``url`` has any trailing slash stripped so the upstream path can be appended
    directly. ``backend_id`` is stable for metrics and logs; it defaults to the url when
    the operator does not supply one. ``weight`` is carried for a future weighted strategy
    and is not consulted by the round-robin selection this feature ships.
    """

    url: str
    backend_id: str
    weight: float = 1.0


class BackendHealth:
    """Health of the backends in one pool, tracked in this process.

    A backend is healthy until it accumulates ``failure_threshold`` consecutive failures,
    at which point it is ejected until ``recovery_seconds`` have passed. A single success
    clears the failure count. In-flight counts are tracked so a later least-in-flight
    strategy has the input it needs; the round-robin selection does not read them.
    """

    def __init__(
        self,
        backends: list[Backend],
        *,
        failure_threshold: int = 3,
        recovery_seconds: float = 30.0,
        clock=time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._failure_threshold = max(1, failure_threshold)
        self._recovery_seconds = max(0.0, recovery_seconds)
        self._clock = clock
        self._failures: dict[str, int] = {b.backend_id: 0 for b in backends}
        # When a backend was ejected; None means it is not ejected.
        self._ejected_at: dict[str, float | None] = {b.backend_id: None for b in backends}
        self._in_flight: dict[str, int] = {b.backend_id: 0 for b in backends}

    def is_available(self, backend: Backend) -> bool:
        """Whether this backend may take a request right now.

        An ejected backend becomes available again once its recovery interval elapses,
        so a host that came back is used without restarting the data plane.
        """
        with self._lock:
            return self._available_locked(backend.backend_id)

    def _available_locked(self, backend_id: str) -> bool:
        ejected_at = self._ejected_at.get(backend_id)
        if ejected_at is None:
            return True
        if self._clock() - ejected_at >= self._recovery_seconds:
            # The cooling interval passed: give it another chance rather than writing it
            # off for the life of the process.
            self._ejected_at[backend_id] = None
            self._failures[backend_id] = 0
            return True
        return False

    def record_success(self, backend: Backend) -> None:
        with self._lock:
            self._failures[backend.backend_id] = 0
            self._ejected_at[backend.backend_id] = None

    def record_failure(self, backend: Backend) -> None:
        """Count a failure and eject the backend once it crosses the threshold."""
        with self._lock:
            count = self._failures.get(backend.backend_id, 0) + 1
            self._failures[backend.backend_id] = count
            if count >= self._failure_threshold:
                self._ejected_at[backend.backend_id] = self._clock()

    def acquire(self, backend: Backend) -> None:
        with self._lock:
            self._in_flight[backend.backend_id] = self._in_flight.get(backend.backend_id, 0) + 1

    def release(self, backend: Backend) -> None:
        with self._lock:
            current = self._in_flight.get(backend.backend_id, 0)
            if current > 0:
                self._in_flight[backend.backend_id] = current - 1

    def snapshot(self) -> dict[str, dict[str, object]]:
        with self._lock:
            return {
                backend_id: {
                    "available": self._available_locked(backend_id),
                    "failures": self._failures.get(backend_id, 0),
                    "in_flight": self._in_flight.get(backend_id, 0),
                }
                for backend_id in self._failures
            }


class BackendPool:
    """The backends serving one deployment plus their health.

    ``select`` returns a healthy backend or ``None`` when every backend is ejected. It is
    round-robin over the currently-healthy backends, which is enough for M1: it spreads
    load and, with health, skips an ejected backend so killing one host does not fail
    requests. A pluggable strategy replaces this method in M2.
    """

    def __init__(self, backends: list[Backend], health: BackendHealth | None = None) -> None:
        if not backends:
            raise ValueError("a deployment must have at least one backend")
        self._backends = list(backends)
        self.health = health or BackendHealth(self._backends)
        self._cycle = itertools.cycle(self._backends)
        self._lock = threading.Lock()

    @property
    def backends(self) -> list[Backend]:
        return list(self._backends)

    def __len__(self) -> int:
        return len(self._backends)

    def select(self, *, exclude: set[str] | None = None) -> Backend | None:
        """Return a healthy backend not in ``exclude``, or ``None`` if none qualifies.

        ``exclude`` lets the caller retry: a backend that just failed this request is
        passed in so the next pick lands elsewhere without waiting for the failure count
        to cross the ejection threshold.
        """
        excluded = exclude or set()
        with self._lock:
            # One full turn of the cycle, so every backend is considered exactly once and
            # the starting point advances between calls (the round-robin part).
            for _ in range(len(self._backends)):
                candidate = next(self._cycle)
                if candidate.backend_id in excluded:
                    continue
                if self.health.is_available(candidate):
                    return candidate
        return None
