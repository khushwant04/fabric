"""Balancing strategies over the backend pool (M2, ADR 0010).

Each strategy chooses among the backends that are healthy right now; health decides
which backends are eligible and the strategy decides how load spreads across them. These
tests pin the selecting behaviour of each strategy and prove none can return an ejected
backend.
"""

from __future__ import annotations

import random

from fabric_data_plane.pool import (
    DEFAULT_STRATEGY,
    Backend,
    BackendHealth,
    BackendPool,
    build_strategy,
)


def _backends(*ids: str) -> list[Backend]:
    return [Backend(url=f"http://{i}", backend_id=i) for i in ids]


def test_the_default_strategy_is_least_in_flight() -> None:
    pool = BackendPool(_backends("a", "b"))
    assert pool.strategy_name == "least_in_flight"
    assert DEFAULT_STRATEGY == "least_in_flight"


def test_an_unknown_strategy_falls_back_to_the_default() -> None:
    """A data plane must not stop serving a deployment over a name a newer control plane
    sent, so an unrecognised strategy defaults rather than raising."""
    pool = BackendPool(_backends("a", "b"), strategy="nonsense")
    assert pool.strategy_name == "least_in_flight"


def test_least_in_flight_prefers_the_least_loaded_backend() -> None:
    backends = _backends("a", "b", "c")
    health = BackendHealth(backends)
    pool = BackendPool(backends, health, strategy="least_in_flight")

    # Load a and b so c is the least busy.
    health.acquire(backends[0])
    health.acquire(backends[0])
    health.acquire(backends[1])
    assert pool.select().backend_id == "c"

    # Now load c past the others; the pick moves to whichever is now lightest.
    health.acquire(backends[2])
    health.acquire(backends[2])
    assert pool.select().backend_id == "b"


def test_least_in_flight_breaks_ties_deterministically() -> None:
    pool = BackendPool(_backends("b", "a", "c"), strategy="least_in_flight")
    # All zero in flight, so the tie-break by backend id makes the choice stable.
    assert {pool.select().backend_id for _ in range(5)} == {"a"}


def test_round_robin_cycles_the_healthy_backends() -> None:
    pool = BackendPool(_backends("a", "b", "c"), strategy="round_robin")
    picks = [pool.select().backend_id for _ in range(6)]
    assert picks == ["a", "b", "c", "a", "b", "c"]


def test_round_robin_skips_an_ejected_backend_without_leaving_a_gap() -> None:
    backends = _backends("a", "b", "c")
    health = BackendHealth(backends, failure_threshold=1, recovery_seconds=1000)
    pool = BackendPool(backends, health, strategy="round_robin")

    health.record_failure(backends[1])  # eject b
    picks = [pool.select().backend_id for _ in range(4)]
    # The rotation is over the survivors, so it never lands on the ejected backend.
    assert set(picks) == {"a", "c"}
    assert "b" not in picks


def test_session_affinity_is_stable_per_key() -> None:
    pool = BackendPool(_backends("a", "b", "c"), strategy="session_affinity")
    first = pool.select(session_key="conversation-42").backend_id
    # The same key returns the same backend every time.
    for _ in range(10):
        assert pool.select(session_key="conversation-42").backend_id == first


def test_session_affinity_spreads_distinct_keys() -> None:
    pool = BackendPool(_backends("a", "b", "c"), strategy="session_affinity")
    landed = {
        pool.select(session_key=f"key-{i}").backend_id for i in range(60)
    }
    # Not every key on one backend: the hash spreads distinct conversations.
    assert len(landed) > 1


def test_session_affinity_fails_over_when_the_pinned_backend_is_ejected() -> None:
    backends = _backends("a", "b", "c")
    health = BackendHealth(backends, failure_threshold=1, recovery_seconds=1000)
    pool = BackendPool(backends, health, strategy="session_affinity")

    key = "sticky-session"
    pinned = pool.select(session_key=key)
    # Eject the pinned backend; the key must fail over to a healthy one, not fail.
    health.record_failure(pinned)
    after = pool.select(session_key=key)
    assert after is not None
    assert after.backend_id != pinned.backend_id
    # And it is stable on the failover target while the original stays out.
    assert pool.select(session_key=key).backend_id == after.backend_id


def test_session_affinity_returns_to_the_original_after_recovery() -> None:
    ticks = [0.0]

    def clock() -> float:
        return ticks[0]

    backends = _backends("a", "b", "c")
    health = BackendHealth(backends, failure_threshold=1, recovery_seconds=30, clock=clock)
    pool = BackendPool(backends, health, strategy="session_affinity")

    key = "sticky"
    pinned = pool.select(session_key=key)
    health.record_failure(pinned)
    assert pool.select(session_key=key).backend_id != pinned.backend_id
    # Once the pinned backend recovers it rejoins the candidate set and the key returns.
    ticks[0] = 31.0
    assert pool.select(session_key=key).backend_id == pinned.backend_id


def test_weighted_respects_weights_over_many_draws() -> None:
    backends = [
        Backend(url="http://a", backend_id="a", weight=1.0),
        Backend(url="http://b", backend_id="b", weight=3.0),
    ]
    # A seeded RNG makes the distribution assertable.
    pool = BackendPool(backends, strategy=None)
    pool._strategy = build_strategy("weighted", pool.health)
    pool._strategy._rng = random.Random(1234)

    counts = {"a": 0, "b": 0}
    for _ in range(4000):
        counts[pool.select().backend_id] += 1

    # b carries three times a's weight, so roughly three times the traffic. Generous
    # bounds so the test is not flaky while still proving the proportion.
    ratio = counts["b"] / counts["a"]
    assert 2.4 < ratio < 3.6


def test_weighted_redistributes_an_ejected_backends_share() -> None:
    backends = [
        Backend(url="http://a", backend_id="a", weight=1.0),
        Backend(url="http://b", backend_id="b", weight=1.0),
    ]
    health = BackendHealth(backends, failure_threshold=1, recovery_seconds=1000)
    pool = BackendPool(backends, health, strategy="weighted")

    health.record_failure(backends[0])  # eject a
    # Every draw now goes to b: a's share is redistributed, not dropped.
    assert {pool.select().backend_id for _ in range(50)} == {"b"}


def test_every_strategy_only_returns_a_healthy_backend() -> None:
    for name in ("least_in_flight", "round_robin", "session_affinity", "weighted"):
        backends = _backends("a", "b")
        health = BackendHealth(backends, failure_threshold=1, recovery_seconds=1000)
        pool = BackendPool(backends, health, strategy=name)
        health.record_failure(backends[0])  # eject a
        for _ in range(20):
            picked = pool.select(session_key="k")
            assert picked is not None
            assert picked.backend_id == "b", name
        # With both ejected, no strategy invents a backend.
        health.record_failure(backends[1])
        assert pool.select(session_key="k") is None, name
