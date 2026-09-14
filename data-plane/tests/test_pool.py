"""The per-deployment backend pool and its health (ADR 0010).

Covers parsing both configuration shapes (a legacy single ``upstream_url`` and a
``backends`` list), health-based skipping of an ejected backend, and that a connection
failure to one backend ejects it and reselects another so a request is not failed while
a healthy backend remains.
"""

from __future__ import annotations

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
    pool = BackendPool(_backends("http://a", "http://b", "http://c"))
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

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.requests.append(host)
        if host in self.dead:
            raise httpx.ConnectError("backend is down", request=request)
        return httpx.Response(
            200,
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
