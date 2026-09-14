"""The per-deployment backend pool and its health (ADR 0010).

Covers parsing both configuration shapes (a legacy single ``upstream_url`` and a
``backends`` list), health-based skipping of an ejected backend, and that a connection
failure to one backend ejects it and reselects another so a request is not failed while
a healthy backend remains.
"""

from __future__ import annotations

import dataclasses
import uuid

import httpx
import pytest

from fabric_data_plane.pool import Backend, BackendHealth, BackendPool
from fabric_data_plane.registry import Deployment, DeploymentRegistry
from tests.conftest import (
    ACCOUNT_A,
    DEPLOYMENT_A,
    SigningKey,
    make_settings,
)

ACCOUNT = uuid.UUID("11111111-1111-1111-1111-111111111111")
DEP = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _payload(entry: dict) -> dict:
    return {"deployments": [entry]}


def test_a_single_upstream_url_loads_as_a_one_backend_pool() -> None:
    registry = DeploymentRegistry.from_payload(
        _payload(
            {
                "deployment_id": str(DEP),
                "account_id": str(ACCOUNT),
                "model_alias": "launch-model",
                "upstream_url": "http://model-host.test/",
            }
        )
    )
    deployment = registry.resolve("launch-model", account_id=ACCOUNT)
    assert len(deployment.backends) == 1
    backend = deployment.backends[0]
    # The trailing slash is stripped so the upstream path appends cleanly.
    assert backend.url == "http://model-host.test"
    assert backend.backend_id == "http://model-host.test"


def test_a_backends_list_loads_and_carries_id_and_weight() -> None:
    registry = DeploymentRegistry.from_payload(
        _payload(
            {
                "deployment_id": str(DEP),
                "account_id": str(ACCOUNT),
                "model_alias": "launch-model",
                "backends": [
                    {"url": "http://a.test/", "id": "pod-a", "weight": 2.0},
                    {"url": "http://b.test", "id": "pod-b"},
                ],
            }
        )
    )
    deployment = registry.resolve("launch-model", account_id=ACCOUNT)
    assert [b.url for b in deployment.backends] == ["http://a.test", "http://b.test"]
    assert [b.backend_id for b in deployment.backends] == ["pod-a", "pod-b"]
    assert deployment.backends[0].weight == 2.0
    # An unspecified weight defaults to 1.
    assert deployment.backends[1].weight == 1.0


def test_strategy_is_parsed_from_the_config_and_defaults_to_least_in_flight() -> None:
    registry = DeploymentRegistry.from_payload(
        _payload(
            {
                "deployment_id": str(DEP),
                "account_id": str(ACCOUNT),
                "model_alias": "launch-model",
                "upstream_url": "http://model-host.test",
                "strategy": "weighted",
            }
        )
    )
    deployment = registry.resolve("launch-model", account_id=ACCOUNT)
    assert deployment.strategy == "weighted"
    assert deployment.build_pool().strategy_name == "weighted"


def test_strategy_defaults_when_absent() -> None:
    registry = DeploymentRegistry.from_payload(
        _payload(
            {
                "deployment_id": str(DEP),
                "account_id": str(ACCOUNT),
                "model_alias": "launch-model",
                "upstream_url": "http://model-host.test",
            }
        )
    )
    deployment = registry.resolve("launch-model", account_id=ACCOUNT)
    assert deployment.strategy == "least_in_flight"
    assert deployment.build_pool().strategy_name == "least_in_flight"


def test_backends_win_when_both_shapes_are_present() -> None:
    registry = DeploymentRegistry.from_payload(
        _payload(
            {
                "deployment_id": str(DEP),
                "account_id": str(ACCOUNT),
                "model_alias": "launch-model",
                "upstream_url": "http://legacy.test",
                "backends": [{"url": "http://a.test"}, {"url": "http://b.test"}],
            }
        )
    )
    deployment = registry.resolve("launch-model", account_id=ACCOUNT)
    assert [b.url for b in deployment.backends] == ["http://a.test", "http://b.test"]


def test_a_deployment_needs_at_least_one_backend() -> None:
    with pytest.raises(ValueError):
        Deployment(
            deployment_id=DEP,
            account_id=ACCOUNT,
            model_alias="launch-model",
        )


def _backends(*urls: str) -> list[Backend]:
    return [Backend(url=u, backend_id=u) for u in urls]


def test_the_pool_round_robins_across_healthy_backends() -> None:
    pool = BackendPool(_backends("http://a", "http://b", "http://c"), strategy="round_robin")
    picks = [pool.select().backend_id for _ in range(6)]
    # Every backend is used and none is starved.
    assert set(picks) == {"http://a", "http://b", "http://c"}
    assert picks.count("http://a") == 2


def test_an_ejected_backend_is_skipped() -> None:
    backends = _backends("http://a", "http://b")
    health = BackendHealth(backends, failure_threshold=1, recovery_seconds=1000)
    pool = BackendPool(backends, health)

    # One failure at threshold=1 ejects backend a.
    pool.health.record_failure(backends[0])
    picks = {pool.select().backend_id for _ in range(4)}
    assert picks == {"http://b"}


def test_all_backends_ejected_returns_none() -> None:
    backends = _backends("http://a")
    health = BackendHealth(backends, failure_threshold=1, recovery_seconds=1000)
    pool = BackendPool(backends, health)
    pool.health.record_failure(backends[0])
    assert pool.select() is None


def test_an_ejected_backend_recovers_after_the_interval() -> None:
    ticks = [0.0]

    def clock() -> float:
        return ticks[0]

    backends = _backends("http://a")
    health = BackendHealth(backends, failure_threshold=1, recovery_seconds=30, clock=clock)
    pool = BackendPool(backends, health)

    pool.health.record_failure(backends[0])
    assert pool.select() is None
    # Before the interval it is still out.
    ticks[0] = 29.0
    assert pool.select() is None
    # After the interval it is tried again rather than written off for the process life.
    ticks[0] = 31.0
    assert pool.select().backend_id == "http://a"


def test_a_success_clears_prior_failures() -> None:
    backends = _backends("http://a")
    health = BackendHealth(backends, failure_threshold=2, recovery_seconds=1000)
    pool = BackendPool(backends, health)

    pool.health.record_failure(backends[0])
    pool.health.record_success(backends[0])
    # The single earlier failure was cleared, so it takes two fresh ones to eject.
    pool.health.record_failure(backends[0])
    assert pool.select() is not None
    pool.health.record_failure(backends[0])
    assert pool.select() is None


def test_exclude_lets_a_caller_reselect_after_a_failure() -> None:
    pool = BackendPool(_backends("http://a", "http://b"))
    first = pool.select()
    second = pool.select(exclude={first.backend_id})
    assert second is not None
    assert second.backend_id != first.backend_id


# --- end-to-end through the ingress -----------------------------------------


class MultiBackendUpstream:
    """A model host reachable at several addresses, some of which can be killed."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.dead: set[str] = set()
        self.ambiguous_failure: set[str] = set()
        self.statuses: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.requests.append(host)
        if host in self.dead:
            raise httpx.ConnectError("backend is down", request=request)
        if host in self.ambiguous_failure:
            raise httpx.ReadTimeout(
                "backend timed out after accepting the request", request=request
            )
        status_code = self.statuses.get(host, 200)
        return httpx.Response(
            status_code,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "model": request.url.host,
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _pool_registry() -> DeploymentRegistry:
    return DeploymentRegistry(
        [
            Deployment(
                deployment_id=DEPLOYMENT_A,
                account_id=ACCOUNT_A,
                model_alias="launch-model",
                upstream_model="internal-release-1",
                backends=(
                    Backend(url="http://a.test", backend_id="pod-a"),
                    Backend(url="http://b.test", backend_id="pod-b"),
                ),
                # Round-robin so sequential requests visibly spread; the M1 health
                # behaviour under test (skip a dead backend, reselect) is independent of
                # which strategy chooses among the healthy ones.
                strategy="round_robin",
            )
        ]
    )


@pytest.fixture
def multi_upstream() -> MultiBackendUpstream:
    return MultiBackendUpstream()


@pytest.fixture
def pool_plane(control_plane, multi_upstream: MultiBackendUpstream):
    from fabric_data_plane.app import DataPlane
    from fabric_data_plane.keys import KeyCache

    settings = make_settings(backend_failure_threshold=1, backend_recovery_seconds=1000)
    return DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=control_plane.client()),
        registry=_pool_registry(),
        client=multi_upstream.client(),
    )


@pytest.fixture
async def pool_client(pool_plane):
    from fabric_data_plane.app import create_inference_app

    app = create_inference_app(pool_plane)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress.test") as http:
        yield http


def _chat() -> dict:
    return {"model": "launch-model", "messages": [{"role": "user", "content": "hi"}]}


async def test_requests_are_spread_across_backends(
    pool_client, signing_key: SigningKey, multi_upstream: MultiBackendUpstream
) -> None:
    for _ in range(4):
        response = await pool_client.post(
            "/v1/chat/completions",
            json=_chat(),
            headers={"Authorization": f"Bearer {signing_key.issue()}"},
        )
        assert response.status_code == 200, response.text
    # Both backends served traffic; neither was starved.
    assert set(multi_upstream.requests) == {"a.test", "b.test"}


async def test_killing_one_backend_does_not_fail_requests(
    pool_client, signing_key: SigningKey, multi_upstream: MultiBackendUpstream
) -> None:
    """A dead backend is ejected and requests continue on the healthy one (ADR 0010)."""
    multi_upstream.dead.add("a.test")

    # Several requests: whichever ones would have gone to the dead backend reselect the
    # healthy one rather than failing.
    for _ in range(6):
        response = await pool_client.post(
            "/v1/chat/completions",
            json=_chat(),
            headers={"Authorization": f"Bearer {signing_key.issue()}"},
        )
        assert response.status_code == 200, response.text
        # Every answer came from the live backend.
        assert response.json()["model"] == "launch-model"

    # The live backend served every successful reply.
    assert multi_upstream.requests.count("b.test") >= 6


async def test_an_ambiguous_failure_is_not_replayed(
    pool_client, signing_key: SigningKey, multi_upstream: MultiBackendUpstream
) -> None:
    """A timeout after acceptance must not execute the completion on a second host."""
    multi_upstream.ambiguous_failure.add("a.test")

    response = await pool_client.post(
        "/v1/chat/completions",
        json=_chat(),
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )

    assert response.status_code == 503
    assert multi_upstream.requests == ["a.test"]


async def test_a_server_error_marks_the_backend_unhealthy_without_replay(
    pool_client, signing_key: SigningKey, multi_upstream: MultiBackendUpstream
) -> None:
    multi_upstream.statuses["a.test"] = 503
    headers = {"Authorization": f"Bearer {signing_key.issue()}"}

    first = await pool_client.post("/v1/chat/completions", json=_chat(), headers=headers)
    second = await pool_client.post("/v1/chat/completions", json=_chat(), headers=headers)
    third = await pool_client.post("/v1/chat/completions", json=_chat(), headers=headers)

    # The explicit 503 is returned rather than replayed, but threshold=1 ejects that
    # backend and subsequent requests keep serving on the healthy replica.
    assert [first.status_code, second.status_code, third.status_code] == [503, 200, 200]
    assert multi_upstream.requests == ["a.test", "b.test", "b.test"]


async def test_a_streamed_server_error_marks_the_backend_unhealthy_without_replay(
    pool_client, signing_key: SigningKey, multi_upstream: MultiBackendUpstream
) -> None:
    multi_upstream.statuses["a.test"] = 503
    headers = {"Authorization": f"Bearer {signing_key.issue()}"}

    streamed = await pool_client.post(
        "/v1/chat/completions",
        json={**_chat(), "stream": True},
        headers=headers,
    )
    second = await pool_client.post("/v1/chat/completions", json=_chat(), headers=headers)
    third = await pool_client.post("/v1/chat/completions", json=_chat(), headers=headers)

    # The stream is never replayed after it starts. Its 503 health signal ejects a.test,
    # so both later requests use b.test rather than resetting a.test to healthy.
    assert streamed.status_code == 200
    assert [second.status_code, third.status_code] == [200, 200]
    assert multi_upstream.requests == ["a.test", "b.test", "b.test"]


async def test_all_backends_dead_surfaces_as_unavailable(
    pool_client, signing_key: SigningKey, multi_upstream: MultiBackendUpstream
) -> None:
    multi_upstream.dead.update({"a.test", "b.test"})
    response = await pool_client.post(
        "/v1/chat/completions",
        json=_chat(),
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "upstream_unavailable"



def test_a_pool_rebuilds_when_a_stable_backend_id_moves(pool_plane) -> None:
    """A pod identity may survive while EndpointSlice publishes a new dial address."""
    before = pool_plane.pool_for(_pool_registry().resolve("launch-model", account_id=ACCOUNT_A))
    moved = Deployment(
        deployment_id=DEPLOYMENT_A,
        account_id=ACCOUNT_A,
        model_alias="launch-model",
        backends=(
            Backend(url="http://a-moved.test", backend_id="pod-a"),
            Backend(url="http://b.test", backend_id="pod-b"),
        ),
    )

    after = pool_plane.pool_for(moved)

    assert after is not before
    assert [backend.url for backend in after.backends] == [
        "http://a-moved.test",
        "http://b.test",
    ]



def test_withdrawn_deployment_pools_and_backend_metrics_are_retired(pool_plane) -> None:
    old_deployment = _pool_registry().resolve("launch-model", account_id=ACCOUNT_A)
    old_pool = pool_plane.pool_for(old_deployment)
    for backend in old_pool.backends:
        pool_plane.metrics.backend_health(
            deployment=str(DEPLOYMENT_A), backend=backend.backend_id, available=True
        )
        pool_plane.metrics.backend_started(
            deployment=str(DEPLOYMENT_A), backend=backend.backend_id
        )
        pool_plane.metrics.backend_finished(
            deployment=str(DEPLOYMENT_A), backend=backend.backend_id, outcome="ok"
        )
    assert "pod-a" in pool_plane.metrics.render()

    replacement_id = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    replacement = Deployment(
        deployment_id=replacement_id,
        account_id=ACCOUNT_A,
        model_alias="replacement-model",
        upstream_url="http://replacement.test",
    )
    pool_plane.registry = DeploymentRegistry([replacement])
    pool_plane.pool_for(replacement)

    assert DEPLOYMENT_A not in pool_plane._pools
    assert replacement_id in pool_plane._pools
    assert "pod-a" not in pool_plane.metrics.render()
    assert "pod-b" not in pool_plane.metrics.render()



def test_unknown_strategy_falls_back_to_least_in_flight() -> None:
    registry = DeploymentRegistry.from_payload(
        _payload(
            {
                "deployment_id": str(DEP),
                "account_id": str(ACCOUNT),
                "model_alias": "launch-model",
                "upstream_url": "http://model-host.test",
                "strategy": "future-strategy",
            }
        )
    )
    assert registry.resolve("launch-model", account_id=ACCOUNT).strategy == "least_in_flight"


@pytest.mark.parametrize("weight", [-1, float("inf"), float("nan")])
def test_invalid_backend_weight_is_rejected(weight: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        DeploymentRegistry.from_payload(
            _payload(
                {
                    "deployment_id": str(DEP),
                    "account_id": str(ACCOUNT),
                    "model_alias": "launch-model",
                    "backends": [{"url": "http://model-host.test", "weight": weight}],
                }
            )
        )


def test_a_pool_rebuilds_strategy_without_losing_health_or_active_load(pool_plane) -> None:
    original = _pool_registry().resolve("launch-model", account_id=ACCOUNT_A)
    before = pool_plane.pool_for(original)
    first = before.backends[0]
    second = before.backends[1]
    before.health.acquire(first)
    # Default threshold is three; cross it so the ejection cooldown is live.
    for _ in range(3):
        before.health.record_failure(second)
    assert before.health.is_available(second) is False

    changed = dataclasses.replace(original, strategy="session_affinity")
    after = pool_plane.pool_for(changed)

    assert after is not before
    assert after.health is before.health
    assert after.strategy_name == "session_affinity"
    assert after.health.in_flight()[first.backend_id] == 1
    assert after.health.is_available(second) is False
    after.health.release(first)



def test_removal_to_empty_tombstones_late_backend_metric_updates(pool_plane) -> None:
    deployment = _pool_registry().resolve("launch-model", account_id=ACCOUNT_A)
    pool = pool_plane.pool_for(deployment)
    backend = pool.backends[0]
    label = str(DEPLOYMENT_A)
    pool.health.acquire(backend)
    pool_plane.metrics.backend_started(deployment=label, backend=backend.backend_id)

    pool_plane.registry = DeploymentRegistry([])
    pool_plane.reconcile_pools()
    assert DEPLOYMENT_A not in pool_plane._pools
    assert backend.backend_id not in pool_plane.metrics.render()

    # An attempt dispatched before withdrawal finishes later. Its release must not
    # resurrect metric labels for a backend that is no longer routable.
    pool.health.release(backend)
    pool_plane.metrics.backend_finished(
        deployment=label, backend=backend.backend_id, outcome="ok"
    )
    pool_plane.metrics.backend_health(
        deployment=label, backend=backend.backend_id, available=True
    )
    assert backend.backend_id not in pool_plane.metrics.render()
# --- weighted rollout: two releases in one pool (M3, ADR 0011) --------------
#
# During a downtime-free release change the operator publishes BOTH releases' backends
# into one deployment entry, each backend carrying its release's share of the weight, and
# forces the weighted strategy so the data plane splits traffic between them. These tests
# use the config document the operator would write and prove the split behaves, and that
# when the new release reaches the whole weight the old release receives nothing.


def _rollout_registry(old_weight: float, new_weight: float) -> DeploymentRegistry:
    """A deployment mid-rollout: an old release (pod-old) and a new one (pod-new), each
    with the operator-assigned weight, served by the weighted strategy."""
    entry = {
        "deployment_id": str(DEPLOYMENT_A),
        "account_id": str(ACCOUNT_A),
        "model_alias": "launch-model",
        "strategy": "weighted",
        "backends": [
            {"url": "http://old.test", "id": "pod-old", "weight": old_weight},
            {"url": "http://new.test", "id": "pod-new", "weight": new_weight},
        ],
    }
    return DeploymentRegistry.from_payload({"deployments": [entry]})


def test_a_two_release_pool_splits_traffic_by_weight() -> None:
    # A 3:1 split toward the new release means it takes roughly three quarters of the
    # traffic while the old release drains, but keeps serving its share.
    import random

    from fabric_data_plane.pool import build_strategy

    registry = _rollout_registry(old_weight=0.25, new_weight=0.75)
    deployment = registry.resolve("launch-model", account_id=ACCOUNT_A)
    pool = deployment.build_pool()
    # A seeded RNG makes the proportion assertable without flakiness.
    pool._strategy = build_strategy("weighted", pool.health)
    pool._strategy._rng = random.Random(20240501)

    counts = {"pod-old": 0, "pod-new": 0}
    for _ in range(4000):
        counts[pool.select().backend_id] += 1

    # Both releases receive traffic; neither is starved while the shift is in progress.
    assert counts["pod-old"] > 0
    assert counts["pod-new"] > 0
    ratio = counts["pod-new"] / counts["pod-old"]
    assert 2.4 < ratio < 3.6


def test_when_the_new_release_reaches_full_weight_the_old_gets_nothing() -> None:
    # The completion of a rollout: the operator drops the drained release from the pool
    # entirely, so the config carries only the new release and it takes everything.
    entry = {
        "deployment_id": str(DEPLOYMENT_A),
        "account_id": str(ACCOUNT_A),
        "model_alias": "launch-model",
        "strategy": "weighted",
        "backends": [{"url": "http://new.test", "id": "pod-new", "weight": 1.0}],
    }
    registry = DeploymentRegistry.from_payload({"deployments": [entry]})
    deployment = registry.resolve("launch-model", account_id=ACCOUNT_A)
    pool = deployment.build_pool()

    picks = {pool.select().backend_id for _ in range(100)}
    # Only the new release is ever chosen: the old release receives nothing.
    assert picks == {"pod-new"}


def _rollout_pool_registry() -> DeploymentRegistry:
    """The mid-rollout deployment reachable through the ingress: old at a.test carries the
    bulk of the weight, new at b.test a smaller share, served by the weighted strategy."""
    return DeploymentRegistry(
        [
            Deployment(
                deployment_id=DEPLOYMENT_A,
                account_id=ACCOUNT_A,
                model_alias="launch-model",
                backends=(
                    Backend(url="http://a.test", backend_id="pod-old", weight=0.6),
                    Backend(url="http://b.test", backend_id="pod-new", weight=0.4),
                ),
                strategy="weighted",
            )
        ]
    )


@pytest.fixture
def rollout_plane(control_plane, multi_upstream: MultiBackendUpstream):
    from fabric_data_plane.app import DataPlane
    from fabric_data_plane.keys import KeyCache

    settings = make_settings(backend_failure_threshold=1, backend_recovery_seconds=1000)
    return DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=control_plane.client()),
        registry=_rollout_pool_registry(),
        client=multi_upstream.client(),
    )


@pytest.fixture
async def rollout_client(rollout_plane):
    from fabric_data_plane.app import create_inference_app

    app = create_inference_app(rollout_plane)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress.test") as http:
        yield http


async def test_no_request_is_dropped_across_a_simulated_release_change(
    rollout_client, signing_key: SigningKey, multi_upstream: MultiBackendUpstream
) -> None:
    """The M3 safety property at the data plane: while both releases are in the pool every
    request is served, and traffic reaches both releases, so a release change serves
    continuously rather than going offline (ADR 0011)."""
    for _ in range(20):
        response = await rollout_client.post(
            "/v1/chat/completions",
            json=_chat(),
            headers={"Authorization": f"Bearer {signing_key.issue()}"},
        )
        # Not one request dropped across the shift.
        assert response.status_code == 200, response.text

    # Both releases carried some of the traffic, so the split is real rather than all
    # landing on one release.
    assert "a.test" in multi_upstream.requests
    assert "b.test" in multi_upstream.requests
