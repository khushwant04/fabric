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

Selection is delegated to a pluggable *strategy* (M2). ``least_in_flight`` is the
default: it sends a request to the healthy backend carrying the fewest in-flight
requests, which tracks real load rather than a position in a ring. ``round_robin``
cycles the healthy backends, ``session_affinity`` pins a session key to a stable backend
so consecutive turns of one conversation reach the same host (failing over when that
host is unhealthy), and ``weighted`` picks proportionally to a per-backend weight. Every
strategy consults the same health tracker, so none can ever return an ejected backend:
``BackendPool.select`` asks the strategy only among the currently-available backends.
"""

from __future__ import annotations

import bisect
import dataclasses
import hashlib
import itertools
import random
import threading
import time

#: Balancing strategies a deployment may choose. ``least_in_flight`` is the default
#: because it responds to real load rather than a fixed rotation, which matters most when
#: backends decode at different rates or a slow request piles up on one host.
STRATEGIES = ("least_in_flight", "round_robin", "session_affinity", "weighted")

#: The default strategy when a deployment does not name one.
DEFAULT_STRATEGY = "least_in_flight"


@dataclasses.dataclass(frozen=True)
class Backend:
    """One model-host endpoint a deployment can be served from.

    ``url`` has any trailing slash stripped so the upstream path can be appended
    directly. ``backend_id`` is stable for metrics and logs; it defaults to the url when
    the operator does not supply one. ``weight`` is consulted by the weighted strategy,
    where a backend with twice the weight takes roughly twice the traffic; the other
    strategies ignore it.
    """

    url: str
    backend_id: str
    weight: float = 1.0


class BackendHealth:
    """Health of the backends in one pool, tracked in this process.

    A backend is healthy until it accumulates ``failure_threshold`` consecutive failures,
    at which point it is ejected until ``recovery_seconds`` have passed. A single success
    clears the failure count. In-flight counts are tracked here so the least-in-flight
    strategy can read them through :meth:`in_flight`.
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

    def in_flight(self) -> dict[str, int]:
        """A snapshot of the in-flight count per backend, for the least-in-flight pick."""
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
    """How a pool chooses among the backends that are healthy right now.

    A strategy never sees an ejected backend: :meth:`BackendPool.select` filters the pool
    by health and hands the strategy only the available candidates, so every strategy
    honours health for free and none can return a host that is down. ``pick`` receives the
    healthy candidates and an optional ``session_key`` (used only by affinity) and returns
    one of them.
    """

    name = "strategy"

    def pick(self, candidates: list[Backend], *, session_key: str | None) -> Backend:
        raise NotImplementedError


class RoundRobinStrategy(Strategy):
    """Cycle the healthy backends so load is spread evenly by count.

    The cursor advances over the *healthy* candidates rather than a fixed ring, so a
    backend that is ejected does not leave a gap the cursor lands in and skips: the
    rotation is over whoever can take a request now.
    """

    name = "round_robin"

    def __init__(self) -> None:
        self._counter = itertools.count()

    def pick(self, candidates: list[Backend], *, session_key: str | None) -> Backend:
        index = next(self._counter) % len(candidates)
        return candidates[index]


class LeastInFlightStrategy(Strategy):
    """Send the request to the healthy backend with the fewest requests in flight.

    This tracks real load rather than a position in a ring: a backend decoding a long
    generation carries its in-flight count until it finishes, so the next request goes
    elsewhere instead of piling onto the busy host. Ties are broken by the backend id so
    the choice is deterministic for a test and does not thrash between equal backends.
    """

    name = "least_in_flight"

    def __init__(self, health: BackendHealth) -> None:
        self._health = health

    def pick(self, candidates: list[Backend], *, session_key: str | None) -> Backend:
        counts = self._health.in_flight()
        return min(candidates, key=lambda b: (counts.get(b.backend_id, 0), b.backend_id))


class SessionAffinityStrategy(Strategy):
    """Pin a session key to a stable backend so a conversation stays on one host.

    Consecutive turns of one conversation carry the same key (a session or user header,
    or the request's own attributes when none is sent), and the same key hashes to the
    same backend, so the turns land together and reuse whatever the host has cached for
    that session. The hash is taken over the healthy candidates, so when the pinned
    backend is ejected the key fails over to another healthy one deterministically rather
    than failing; it returns to the original once that host recovers and rejoins the
    candidate set. With no key it falls back to round-robin so a stream of anonymous
    requests is still spread.
    """

    name = "session_affinity"

    def __init__(self) -> None:
        self._counter = itertools.count()

    def pick(self, candidates: list[Backend], *, session_key: str | None) -> Backend:
        if not session_key:
            return candidates[next(self._counter) % len(candidates)]
        # Sorted so the mapping depends only on the set of healthy candidates, not the
        # order they arrived in, and a stable digest so it does not move between processes
        # or Python's per-run hash seed.
        ordered = sorted(candidates, key=lambda b: b.backend_id)
        digest = hashlib.sha256(session_key.encode("utf-8")).digest()
        index = int.from_bytes(digest[:8], "big") % len(ordered)
        return ordered[index]


class WeightedStrategy(Strategy):
    """Choose proportionally to each backend's weight over many requests.

    A backend with twice the weight takes roughly twice the traffic, which is how a fleet
    of uneven hosts (a larger GPU beside a smaller one) is balanced by capacity rather
    than by count. The draw is over the healthy candidates only, so ejecting a backend
    redistributes its share across the rest rather than dropping the requests that would
    have gone to it. A non-positive weight is treated as a small positive one so a
    misconfigured backend still receives the occasional probe rather than being silently
    starved.
    """

    name = "weighted"

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()

    def pick(self, candidates: list[Backend], *, session_key: str | None) -> Backend:
        weights = [w if (w := b.weight) > 0 else 1e-9 for b in candidates]
        # Cumulative weights plus a uniform draw is O(log n) and, unlike random.choices,
        # lets an injected RNG make the distribution assertable in a test.
        cumulative: list[float] = []
        running = 0.0
        for weight in weights:
            running += weight
            cumulative.append(running)
        point = self._rng.random() * running
        index = bisect.bisect_left(cumulative, point)
        if index >= len(candidates):
            index = len(candidates) - 1
        return candidates[index]


def build_strategy(name: str | None, health: BackendHealth) -> Strategy:
    """Build the named strategy, defaulting to least-in-flight for an unknown name.

    An unrecognised name defaults rather than raising, matching how the agent treats an
    unknown kernel mode: the vocabulary is validated upstream, and a data plane that
    refused a value a newer control plane sent would stop serving a deployment entirely.
    """
    match name:
        case "round_robin":
            return RoundRobinStrategy()
        case "session_affinity":
            return SessionAffinityStrategy()
        case "weighted":
            return WeightedStrategy()
        case _:
            return LeastInFlightStrategy(health)


class BackendPool:
    """The backends serving one deployment, their health, and a balancing strategy.

    ``select`` returns a healthy backend or ``None`` when every backend is ejected. It
    filters the backends by health first and asks the strategy only among the survivors,
    so the strategy chooses *how* to spread load while health decides *which* backends are
    eligible. ``exclude`` drops a backend that just failed this request so a retry lands
    elsewhere without waiting for the failure count to cross the ejection threshold.
    """

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
        if isinstance(strategy, Strategy):
            self._strategy = strategy
        else:
            self._strategy = build_strategy(strategy, self.health)
        self._lock = threading.Lock()

    @property
    def backends(self) -> list[Backend]:
        return list(self._backends)

    @property
    def strategy_name(self) -> str:
        return self._strategy.name

    def __len__(self) -> int:
        return len(self._backends)

    def select(
        self, *, exclude: set[str] | None = None, session_key: str | None = None
    ) -> Backend | None:
        """Return a healthy backend not in ``exclude``, or ``None`` if none qualifies.

        The pool is filtered to the backends that are available now and not excluded, and
        the strategy picks among those. ``session_key`` is consulted only by the affinity
        strategy; the others ignore it.
        """
        excluded = exclude or set()
        with self._lock:
            candidates = [
                backend
                for backend in self._backends
                if backend.backend_id not in excluded and self.health.is_available(backend)
            ]
            if not candidates:
                return None
            return self._strategy.pick(candidates, session_key=session_key)
