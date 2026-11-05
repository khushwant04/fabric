"""Per-deployment model-host pools, health, and routing strategies.

ADR 0010 puts concrete backend discovery in the operator and balancing in the data
plane. ADR 0011 defines the per-deployment strategies, and ADR 0019 adds prefix-aware
routing. Health eligibility is always applied before strategy selection, so no strategy
can return an ejected backend.
"""

from __future__ import annotations

import bisect
import dataclasses
import hashlib
import itertools
import math
import random
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence

STRATEGIES = ("least_in_flight", "round_robin", "session_affinity", "weighted", "prefix_aware")
DEFAULT_STRATEGY = "least_in_flight"
#: Prefix-aware routing's load bound (ADR 0019). 1.1 kept 88% of session affinity's
#: cache hits while matching least-in-flight's tail latency in the routing benchmark;
#: 1.25 and above traded tail latency for a few more hits.
DEFAULT_PREFIX_LOAD_FACTOR = 1.1


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

    def forget(self, backend_ids: set[str]) -> None:
        """Drop any per-backend state for backends that left the pool."""
    #: Whether ``pick`` reads the request's prompt-prefix block hashes. The gateway
    #: computes them only for a strategy that does.
    uses_prefix = False

    def pick(
        self,
        candidates: list[Backend],
        *,
        session_key: str | None,
        prefix: Sequence[bytes] | None = None,
    ) -> Backend:
        raise NotImplementedError


class RoundRobinStrategy(Strategy):
    name = "round_robin"

    def __init__(self) -> None:
        self._counter = itertools.count()

    def pick(
        self,
        candidates: list[Backend],
        *,
        session_key: str | None,
        prefix: Sequence[bytes] | None = None,
    ) -> Backend:
        return candidates[next(self._counter) % len(candidates)]


class LeastInFlightStrategy(Strategy):
    """Choose the least loaded backend and rotate equal-load ties."""

    name = "least_in_flight"

    def __init__(self, health: BackendHealth) -> None:
        self._health = health
        self._counter = itertools.count()

    def pick(
        self,
        candidates: list[Backend],
        *,
        session_key: str | None,
        prefix: Sequence[bytes] | None = None,
    ) -> Backend:
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

    def pick(
        self,
        candidates: list[Backend],
        *,
        session_key: str | None,
        prefix: Sequence[bytes] | None = None,
    ) -> Backend:
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

    def pick(
        self,
        candidates: list[Backend],
        *,
        session_key: str | None,
        prefix: Sequence[bytes] | None = None,
    ) -> Backend:
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


class PrefixAwareStrategy(Strategy):
    """Send a request where its prompt prefix is likely cached, within a load bound.

    The gateway keeps an approximate index from prompt-prefix block hashes (see
    ``prefix.py``) to the backends that recently served them; it is a guess at each
    engine's KV cache, refreshed by every request it routes. A request goes to the
    backend matching the longest run of its leading blocks, but only among backends whose
    in-flight count stays within ``load_factor`` times the pool mean (consistent hashing
    with bounded loads). Without a match it goes to the least loaded backend. So a
    conversation returns to the host holding its history, a shared system prompt spreads
    across the hosts that hold it, and no host takes more than its bounded share, which
    is how session affinity failed in the baseline benchmark.
    """

    name = "prefix_aware"
    uses_prefix = True

    def __init__(
        self,
        health: BackendHealth,
        *,
        capacity_blocks: int = 65_536,
        load_factor: float = DEFAULT_PREFIX_LOAD_FACTOR,
        owners_per_block: int = 4,
    ) -> None:
        self._health = health
        self._counter = itertools.count()
        self._capacity = max(1, capacity_blocks)
        self._load_factor = max(1.0, load_factor)
        self._owners_per_block = max(1, owners_per_block)
        # Block hash -> backend ids that served it, most recent last. Least recently
        # routed blocks are evicted first, as an engine's cache evicts them.
        self._index: OrderedDict[bytes, list[str]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._index)

    def pick(
        self,
        candidates: list[Backend],
        *,
        session_key: str | None,
        prefix: Sequence[bytes] | None = None,
    ) -> Backend:
        counts = self._health.in_flight()
        load = {backend.backend_id: counts.get(backend.backend_id, 0) for backend in candidates}
        # Counting this request, no backend may exceed load_factor times the mean. The
        # least loaded backend always fits: its load is at most the floor of the mean.
        cap = math.ceil(self._load_factor * (sum(load.values()) + 1) / len(candidates))
        eligible = [backend for backend in candidates if load[backend.backend_id] + 1 <= cap]
        eligible = eligible or candidates

        matched = self.matched_blocks(prefix or (), [b.backend_id for b in eligible])
        best = max(matched.values(), default=0)
        pool = [b for b in eligible if matched.get(b.backend_id, 0) == best] if best else eligible
        lightest = min(load[backend.backend_id] for backend in pool)
        tied = sorted(
            (backend for backend in pool if load[backend.backend_id] == lightest),
            key=lambda backend: backend.backend_id,
        )
        chosen = tied[next(self._counter) % len(tied)]
        self._record(prefix or (), chosen.backend_id)
        return chosen

    def matched_blocks(self, prefix: Sequence[bytes], backend_ids: list[str]) -> dict[str, int]:
        """How many leading blocks of ``prefix`` each backend is believed to hold."""
        alive = set(backend_ids)
        matched = dict.fromkeys(backend_ids, 0)
        for depth, block in enumerate(prefix, start=1):
            owners = self._index.get(block)
            if not owners:
                break
            alive &= set(owners)
            if not alive:
                break
            for backend_id in alive:
                matched[backend_id] = depth
        return matched

    def _record(self, prefix: Sequence[bytes], backend_id: str) -> None:
        for block in prefix:
            owners = self._index.get(block)
            if owners is None:
                self._index[block] = [backend_id]
            else:
                if backend_id in owners:
                    owners.remove(backend_id)
                owners.append(backend_id)
                del owners[: -self._owners_per_block]
                self._index.move_to_end(block)
        while len(self._index) > self._capacity:
            self._index.popitem(last=False)

    def forget(self, backend_ids: set[str]) -> None:
        """Drop backends that left the pool, so their entries stop counting as matches."""
        for block in list(self._index):
            owners = [owner for owner in self._index[block] if owner not in backend_ids]
            if owners:
                self._index[block] = owners
            else:
                del self._index[block]


def build_strategy(
    name: str | None,
    health: BackendHealth,
    *,
    prefix_load_factor: float = DEFAULT_PREFIX_LOAD_FACTOR,
) -> Strategy:
    match name:
        case "round_robin":
            return RoundRobinStrategy()
        case "session_affinity":
            return SessionAffinityStrategy()
        case "weighted":
            return WeightedStrategy()
        case "prefix_aware":
            return PrefixAwareStrategy(health, load_factor=prefix_load_factor)
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
        *,
        prefix_load_factor: float = DEFAULT_PREFIX_LOAD_FACTOR,
    ) -> None:
        if not backends:
            raise ValueError("a deployment must have at least one backend")
        self._backends = list(backends)
        self.health = health or BackendHealth(self._backends)
        self._strategy = (
            strategy
            if isinstance(strategy, Strategy)
            else build_strategy(strategy, self.health, prefix_load_factor=prefix_load_factor)
        )
        self._lock = threading.Lock()

    @property
    def backends(self) -> list[Backend]:
        return list(self._backends)

    @property
    def strategy_name(self) -> str:
        return self._strategy.name

    @property
    def strategy(self) -> Strategy:
        return self._strategy

    @property
    def uses_prefix(self) -> bool:
        """Whether ``select`` should be given the request's prompt-prefix block hashes."""
        return self._strategy.uses_prefix

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
        self,
        *,
        exclude: set[str] | None = None,
        session_key: str | None = None,
        prefix: Sequence[bytes] | None = None,
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
            return self._strategy.pick(candidates, session_key=session_key, prefix=prefix)
