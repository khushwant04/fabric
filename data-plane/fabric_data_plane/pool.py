"""Per-deployment model-host pools, health, and routing strategies.

ADR 0010 puts concrete backend discovery in the operator and balancing in the data
plane. ADR 0011 defines the per-deployment strategies. Health eligibility is always
applied before strategy selection, so no strategy can return an ejected backend.
"""

from __future__ import annotations

import bisect
import dataclasses
import hashlib
import itertools
import random
import threading
import time

STRATEGIES = ("least_in_flight", "round_robin", "session_affinity", "weighted")
DEFAULT_STRATEGY = "least_in_flight"


@dataclasses.dataclass(frozen=True)
class Backend:
    """One model-host endpoint.

    ``backend_id`` is stable across address changes and is used for health, affinity,
    and metrics. ``weight`` is consulted only by weighted routing; zero drains the
    backend without marking it unhealthy.
    """

    url: str
    backend_id: str
    weight: float = 1.0
    workload: str = ""


class BackendHealth:
    """Process-local health and active-attempt state for one backend pool."""

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
        self._failures: dict[str, int] = {backend.backend_id: 0 for backend in backends}
        self._ejected_at: dict[str, float | None] = {
            backend.backend_id: None for backend in backends
        }
        self._in_flight: dict[str, int] = {backend.backend_id: 0 for backend in backends}
        self._workloads: dict[str, str] = {
            backend.backend_id: backend.workload for backend in backends
        }

    def is_available(self, backend: Backend) -> bool:
        with self._lock:
            return self._available_locked(backend.backend_id)

    def _available_locked(self, backend_id: str) -> bool:
        ejected_at = self._ejected_at.get(backend_id)
        if ejected_at is None:
            return True
        if self._clock() - ejected_at >= self._recovery_seconds:
            self._ejected_at[backend_id] = None
            self._failures[backend_id] = 0
            return True
        return False

    def record_success(self, backend: Backend) -> None:
        with self._lock:
            self._failures[backend.backend_id] = 0
            self._ejected_at[backend.backend_id] = None

    def record_failure(self, backend: Backend) -> bool:
        """Record a failure and return whether it newly ejected the backend."""
        with self._lock:
            backend_id = backend.backend_id
            was_ejected = self._ejected_at.get(backend_id) is not None
            count = self._failures.get(backend_id, 0) + 1
            self._failures[backend_id] = count
            if count >= self._failure_threshold:
                self._ejected_at[backend_id] = self._clock()
            return not was_ejected and self._ejected_at.get(backend_id) is not None

    def acquire(self, backend: Backend) -> None:
        with self._lock:
            backend_id = backend.backend_id
            self._in_flight[backend_id] = self._in_flight.get(backend_id, 0) + 1

    def release(self, backend: Backend) -> None:
        with self._lock:
            backend_id = backend.backend_id
            current = self._in_flight.get(backend_id, 0)
            if current > 0:
                self._in_flight[backend_id] = current - 1

    def reconcile(self, backends: list[Backend]) -> None:
        """Preserve overlapping health/load state while pool membership changes."""
        wanted = {backend.backend_id for backend in backends}
        with self._lock:
            for backend in backends:
                backend_id = backend.backend_id
                self._failures.setdefault(backend_id, 0)
                self._ejected_at.setdefault(backend_id, None)
                self._in_flight.setdefault(backend_id, 0)
                if backend.workload:
                    self._workloads[backend_id] = backend.workload
            for backend_id in set(self._failures) - wanted:
                if self._in_flight.get(backend_id, 0) == 0:
                    self._failures.pop(backend_id, None)
                    self._ejected_at.pop(backend_id, None)
                    self._in_flight.pop(backend_id, None)
                    self._workloads.pop(backend_id, None)

    def workloads(self) -> dict[str, str]:
        with self._lock:
            return dict(self._workloads)

    def in_flight(self) -> dict[str, int]:
        with self._lock:
            return dict(self._in_flight)

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


class Strategy:
    name = "strategy"

    def pick(self, candidates: list[Backend], *, session_key: str | None) -> Backend:
        raise NotImplementedError


class RoundRobinStrategy(Strategy):
    name = "round_robin"

    def __init__(self) -> None:
        self._counter = itertools.count()

    def pick(self, candidates: list[Backend], *, session_key: str | None) -> Backend:
        return candidates[next(self._counter) % len(candidates)]


class LeastInFlightStrategy(Strategy):
    """Choose the least loaded backend and rotate equal-load ties."""

    name = "least_in_flight"

    def __init__(self, health: BackendHealth) -> None:
        self._health = health
        self._counter = itertools.count()

    def pick(self, candidates: list[Backend], *, session_key: str | None) -> Backend:
        counts = self._health.in_flight()
        minimum = min(counts.get(backend.backend_id, 0) for backend in candidates)
        tied = sorted(
            (
                backend
                for backend in candidates
                if counts.get(backend.backend_id, 0) == minimum
            ),
            key=lambda backend: backend.backend_id,
        )
        return tied[next(self._counter) % len(tied)]


class SessionAffinityStrategy(Strategy):
    """Use rendezvous hashing for keyed traffic and rotation for anonymous traffic."""

    name = "session_affinity"

    def __init__(self) -> None:
        self._counter = itertools.count()

    def pick(self, candidates: list[Backend], *, session_key: str | None) -> Backend:
        if not session_key:
            return candidates[next(self._counter) % len(candidates)]

        def score(backend: Backend) -> bytes:
            value = f"{session_key}\0{backend.backend_id}".encode()
            return hashlib.sha256(value).digest()

        # Rendezvous hashing remaps only keys that selected a removed backend, unlike
        # modulo hashing, while remaining stable across processes and Python hash seeds.
        return max(candidates, key=lambda backend: (score(backend), backend.backend_id))


class WeightedStrategy(Strategy):
    name = "weighted"

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()

    def pick(self, candidates: list[Backend], *, session_key: str | None) -> Backend:
        # BackendPool removes zero-weight candidates. Registry validation rejects
        # negative or non-finite values, so every value here is positive and finite.
        cumulative: list[float] = []
        running = 0.0
        for backend in candidates:
            running += backend.weight
            cumulative.append(running)
        point = self._rng.random() * running
        index = bisect.bisect_left(cumulative, point)
        return candidates[min(index, len(candidates) - 1)]


def build_strategy(name: str | None, health: BackendHealth) -> Strategy:
    match name:
        case "round_robin":
            return RoundRobinStrategy()
        case "session_affinity":
            return SessionAffinityStrategy()
        case "weighted":
            return WeightedStrategy()
        case _:
            # Unknown values fail open to the validated default so a newer control plane
            # cannot stop an older data plane from serving.
            return LeastInFlightStrategy(health)


class BackendPool:
    """Backends, health, and the strategy selecting among eligible candidates."""

    def __init__(
        self,
        backends: list[Backend],
        health: BackendHealth | None = None,
        strategy: Strategy | str | None = None,
    ) -> None:
        if not backends:
            raise ValueError("a deployment must have at least one backend")
        self._backends = list(backends)
        self.health = health or BackendHealth(self._backends)
        self._strategy = (
            strategy if isinstance(strategy, Strategy) else build_strategy(strategy, self.health)
        )
        self._lock = threading.Lock()

    @property
    def backends(self) -> list[Backend]:
        return list(self._backends)

    @property
    def strategy_name(self) -> str:
        return self._strategy.name

    def __len__(self) -> int:
        return len(self._backends)

    def has_available(self) -> bool:
        """Whether at least one backend is eligible without advancing a strategy cursor."""
        with self._lock:
            candidates = [
                backend for backend in self._backends if self.health.is_available(backend)
            ]
            if self._strategy.name == "weighted":
                candidates = [backend for backend in candidates if backend.weight > 0]
            return bool(candidates)

    def select(
        self, *, exclude: set[str] | None = None, session_key: str | None = None
    ) -> Backend | None:
        excluded = exclude or set()
        with self._lock:
            candidates = [
                backend
                for backend in self._backends
                if backend.backend_id not in excluded and self.health.is_available(backend)
            ]
            if self._strategy.name == "weighted":
                # Zero is an explicit drain signal, not a tiny chance of traffic.
                candidates = [backend for backend in candidates if backend.weight > 0]
            if not candidates:
                return None
            return self._strategy.pick(candidates, session_key=session_key)
