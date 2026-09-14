"""Router obligations that cut across balancing strategies (M2, ADR 0010).

Retry on connection failure (never on a partially streamed response), health-based
ejection with recovery, and per-backend metrics, all exercised through the ingress.
"""

from __future__ import annotations

import httpx
import pytest

from fabric_data_plane.pool import Backend
from fabric_data_plane.registry import Deployment, DeploymentRegistry
from tests.conftest import (
    ACCOUNT_A,
    DEPLOYMENT_A,
    SigningKey,
    make_settings,
)


class _BrokenStream(httpx.AsyncByteStream):
    """A stream that emits one chunk and then fails, to model a mid-stream break.

    The first chunk reaches the client; the failure that follows cannot be transparently
    retried because bytes have already been sent, which is the behaviour under test.
    """

    def __init__(self, request: httpx.Request) -> None:
        self._request = request

    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.ReadError("stream broke", request=self._request)


class FleetUpstream:
    """A pool of model-host addresses, any of which can be killed, with streaming.

    Records which backend host each request hit and how many bytes it managed to send on
    a streamed call, so a test can assert a partially streamed response is not replayed.
    """

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.dead: set[str] = set()
        #: A host that fails only after emitting the first chunk of a stream.
        self.fail_mid_stream: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.requests.append(host)
        if host in self.dead:
            raise httpx.ConnectError("backend is down", request=request)

        import json

        payload = json.loads(request.content or b"{}")
        if payload.get("stream"):
            if host in self.fail_mid_stream:
                return httpx.Response(200, stream=_BrokenStream(request))
            body = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
            return httpx.Response(200, content=body)

        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "model": host,
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _registry(strategy: str = "round_robin") -> DeploymentRegistry:
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
                strategy=strategy,
            )
        ]
    )


@pytest.fixture
def fleet() -> FleetUpstream:
    return FleetUpstream()


def _make_plane(fleet: FleetUpstream, control_plane, **settings_overrides):
    from fabric_data_plane.app import DataPlane
    from fabric_data_plane.keys import KeyCache

    defaults = {"backend_failure_threshold": 1, "backend_recovery_seconds": 1000}
    defaults.update(settings_overrides)
    settings = make_settings(**defaults)
    return DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=control_plane.client()),
        registry=_registry(),
        client=fleet.client(),
    )


@pytest.fixture
def router_plane(control_plane, fleet: FleetUpstream):
    return _make_plane(fleet, control_plane)


@pytest.fixture
async def router_client(router_plane):
    from fabric_data_plane.app import create_inference_app

    app = create_inference_app(router_plane)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress.test") as http:
        yield http


def _chat(**extra) -> dict:
    return {"model": "launch-model", "messages": [{"role": "user", "content": "hi"}], **extra}


def _auth(signing_key: SigningKey) -> dict[str, str]:
    return {"Authorization": f"Bearer {signing_key.issue()}"}


# --- non-streamed retry ------------------------------------------------------


async def test_non_streamed_retries_another_backend_on_connection_failure(
    router_client, signing_key: SigningKey, fleet: FleetUpstream
) -> None:
    """A connection failure to one backend retries the request on a healthy one."""
    fleet.dead.add("a.test")

    response = await router_client.post(
        "/v1/chat/completions", json=_chat(), headers=_auth(signing_key)
    )
    assert response.status_code == 200, response.text
    # The dead backend was attempted at least once and the live one answered.
    assert "b.test" in fleet.requests


async def test_non_streamed_all_dead_surfaces_unavailable_not_a_loop(
    router_client, signing_key: SigningKey, fleet: FleetUpstream
) -> None:
    fleet.dead.update({"a.test", "b.test"})
    response = await router_client.post(
        "/v1/chat/completions", json=_chat(), headers=_auth(signing_key)
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "upstream_unavailable"
    # Bounded: it did not try forever. With two backends and a small budget the attempt
    # count is a handful, not unbounded.
    assert len(fleet.requests) <= 3


async def test_non_streamed_retry_is_bounded_by_max_attempts(
    control_plane, fleet: FleetUpstream, signing_key: SigningKey
) -> None:
    """With a budget of one, a single connection failure is not retried."""
    from fabric_data_plane.app import create_inference_app

    plane = _make_plane(fleet, control_plane, backend_max_attempts=1)
    fleet.dead.add("a.test")
    # Force the first pick onto the dead backend deterministically.
    plane.registry = _registry(strategy="session_affinity")

    app = create_inference_app(plane)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress.test") as http:
        # Try keys until one lands on the dead backend on the first pick.
        got_unavailable = False
        for i in range(20):
            fleet.requests.clear()
            resp = await http.post(
                "/v1/chat/completions",
                json=_chat(),
                headers={"Authorization": f"Bearer {signing_key.issue()}", "x-session-id": f"k{i}"},
            )
            if resp.status_code == 503:
                got_unavailable = True
                # Exactly one attempt was made: the budget forbade a retry.
                assert fleet.requests == ["a.test"]
                break
    assert got_unavailable, "no session key landed on the dead backend for the budget test"


# --- streamed: never retried after the first byte ----------------------------


async def test_streamed_is_not_retried_after_the_first_byte(
    router_client, signing_key: SigningKey, fleet: FleetUpstream
) -> None:
    """A partially streamed response must not be replayed on another backend."""
    fleet.fail_mid_stream.update({"a.test", "b.test"})

    response = await router_client.post(
        "/v1/chat/completions", json=_chat(stream=True), headers=_auth(signing_key)
    )
    assert response.status_code == 200
    # The partial chunk reached the client, then an in-stream error event, and no replay:
    # exactly one backend was contacted.
    assert b"partial" in response.content
    assert b"upstream_unavailable" in response.content
    assert len(fleet.requests) == 1


async def test_streamed_reselects_before_the_first_byte(
    router_client, signing_key: SigningKey, fleet: FleetUpstream
) -> None:
    """A connection failure before any byte is sent may reselect another backend."""
    fleet.dead.add("a.test")

    response = await router_client.post(
        "/v1/chat/completions", json=_chat(stream=True), headers=_auth(signing_key)
    )
    assert response.status_code == 200
    # It fell over to the live backend and streamed a real reply, not an error event.
    assert b"delta" in response.content
    assert b"upstream_unavailable" not in response.content
    assert "b.test" in fleet.requests


# --- ejection and recovery ---------------------------------------------------


async def test_a_backend_is_ejected_after_the_threshold_and_restored_after_the_window(
    control_plane, fleet: FleetUpstream, signing_key: SigningKey
) -> None:
    from fabric_data_plane.app import create_inference_app

    # Threshold of 2 so one failure does not yet eject; a short window so recovery is
    # observable within the test.
    plane = _make_plane(
        fleet, control_plane, backend_failure_threshold=2, backend_recovery_seconds=1000
    )
    # Reach into the memoised pool to drive its clock deterministically.
    deployment = plane.registry.resolve("launch-model", account_id=ACCOUNT_A)
    pool = plane.pool_for(deployment)
    ticks = [0.0]
    pool.health._clock = lambda: ticks[0]
    pool.health._recovery_seconds = 30.0

    app = create_inference_app(plane)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress.test") as http:
        fleet.dead.add("a.test")
        # Enough requests that pod-a is selected and fails twice, crossing the threshold.
        for _ in range(8):
            resp = await http.post(
                "/v1/chat/completions", json=_chat(), headers=_auth(signing_key)
            )
            assert resp.status_code == 200, resp.text

    # pod-a crossed the failure threshold and is ejected; an ejection metric was emitted.
    assert pool.health.is_available(Backend(url="http://a.test", backend_id="pod-a")) is False
    assert 'backend="pod-a"' in plane.metrics.render()
    assert "fabric_dp_backend_ejections_total" in plane.metrics.render()

    # The host comes back; after the recovery window it is probed again.
    fleet.dead.discard("a.test")
    assert pool.health.is_available(Backend(url="http://a.test", backend_id="pod-a")) is False
    ticks[0] = 31.0
    assert pool.health.is_available(Backend(url="http://a.test", backend_id="pod-a")) is True


# --- per-backend metrics -----------------------------------------------------


async def test_per_backend_metrics_appear_in_the_rendered_output(
    router_client, router_plane, signing_key: SigningKey, fleet: FleetUpstream
) -> None:
    for _ in range(4):
        resp = await router_client.post(
            "/v1/chat/completions", json=_chat(), headers=_auth(signing_key)
        )
        assert resp.status_code == 200, resp.text

    rendered = router_plane.metrics.render()
    # Requests are labelled by backend in addition to deployment, so a strategy's spread
    # is measurable.
    assert "fabric_dp_backend_requests_total" in rendered
    assert 'backend="pod-a"' in rendered
    assert 'backend="pod-b"' in rendered
    assert "fabric_dp_backend_in_flight" in rendered
