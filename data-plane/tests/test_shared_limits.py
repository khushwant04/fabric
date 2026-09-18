"""Stamp-local rate and concurrency enforcement shared by independent gateways."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import httpx
import pytest

from fabric_data_plane.app import DataPlane, create_inference_app, create_limit_coordinator_app
from fabric_data_plane.keys import KeyCache
from fabric_data_plane.limits import RateLimit
from fabric_data_plane.shared_limits import (
    LimitCoordinatorStore,
    LimitCoordinatorUnavailable,
    SharedLimitManager,
)
from tests.conftest import (
    ACCOUNT_A,
    ACCOUNT_B,
    SigningKey,
    UpstreamStub,
    make_registry,
    make_settings,
)

TOKEN = "stamp-private-limit-token"


class Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def store(
    tmp_path,
    *,
    rate: int = 0,
    burst: int = 0,
    maximum: int = 0,
    lease: float = 2.0,
    wall_clock=None,
    monotonic_clock=None,
):
    return LimitCoordinatorStore(
        path=str(tmp_path / "private" / "limits.db"),
        rate=RateLimit(requests_per_minute=rate, burst=burst),
        maximum=maximum,
        lease_seconds=lease,
        wall_clock=wall_clock or time.time,
        monotonic_clock=monotonic_clock or time.monotonic,
    )


def app_for(authority: LimitCoordinatorStore):
    settings = make_settings(
        rate_limit_requests_per_minute=authority.rate.requests_per_minute,
        rate_limit_burst=authority.rate.burst,
        max_in_flight_per_account=authority.maximum,
        limit_coordinator_store_path=str(authority._path),
        limit_coordinator_token=TOKEN,
    )
    plane = object.__new__(DataPlane)
    plane.limit_store = authority
    plane.settings = settings
    return create_limit_coordinator_app(plane)


def manager(
    app,
    *,
    rate: bool = False,
    concurrency: bool = False,
    renew: float = 0.1,
) -> SharedLimitManager:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://limits.test",
    )
    return SharedLimitManager(
        base_url="http://limits.test",
        token=TOKEN,
        timeout=1.0,
        renew_seconds=renew,
        rate_enabled=rate,
        concurrency_enabled=concurrency,
        client=client,
    )


async def test_two_gateway_clients_share_one_rate_bucket(tmp_path) -> None:
    authority = store(tmp_path, rate=60, burst=1)
    app = app_for(authority)
    first = manager(app, rate=True)
    second = manager(app, rate=True)

    assert (await first.acquire(ACCOUNT_A)).allowed
    refused = await second.acquire(ACCOUNT_A)
    assert refused.allowed is False
    assert refused.reason == "rate_limited"
    assert refused.retry_after is not None and 0 < refused.retry_after <= 1.0
    # Another tenant owns another bucket.
    assert (await second.acquire(ACCOUNT_B)).allowed


async def test_concurrent_gateways_never_exceed_the_shared_cap(tmp_path) -> None:
    authority = store(tmp_path, maximum=2)
    app = app_for(authority)
    gateways = [manager(app, concurrency=True) for _ in range(6)]

    decisions = await asyncio.gather(*(gateway.acquire(ACCOUNT_A) for gateway in gateways))

    allowed_pairs = [
        (gateway, decision)
        for gateway, decision in zip(gateways, decisions, strict=True)
        if decision.allowed
    ]
    refused = [decision for decision in decisions if not decision.allowed]
    assert len(allowed_pairs) == 2
    assert len(refused) == 4
    assert {decision.reason for decision in refused} == {"too_many_in_flight"}
    other_account = await gateways[3].acquire(ACCOUNT_B)
    assert other_account.allowed
    state = await authority.snapshot()
    assert state["concurrency"]["in_flight"] == 3
    assert state["concurrency"]["accounts_in_flight"] == 2

    await allowed_pairs[0][0].release(allowed_pairs[0][1])
    replacement = await gateways[2].acquire(ACCOUNT_A)
    assert replacement.allowed
    await gateways[2].release(replacement)
    await allowed_pairs[1][0].release(allowed_pairs[1][1])
    await gateways[3].release(other_account)


async def test_admission_request_id_is_idempotent(tmp_path) -> None:
    clock = Clock(100.0)
    authority = store(
        tmp_path,
        rate=60,
        burst=1,
        maximum=1,
        wall_clock=clock,
        monotonic_clock=clock,
    )
    request_id = uuid.uuid4()

    first = await authority.acquire(ACCOUNT_A, request_id)
    clock.value = 100.5
    repeated = await authority.acquire(ACCOUNT_A, request_id)

    assert repeated == first
    assert first.allowed and first.lease_id == request_id
    state = await authority.snapshot()
    assert state["concurrency"]["in_flight"] == 1
    assert state["rate_limit"]["allowed"] == 1


async def test_lease_renewal_prevents_expiry_and_release_is_idempotent(tmp_path) -> None:
    authority = store(tmp_path, maximum=1, lease=0.25)
    app = app_for(authority)
    gateway = manager(app, concurrency=True, renew=0.05)

    admission = await gateway.acquire(ACCOUNT_A)
    assert admission.allowed
    await asyncio.sleep(0.4)
    assert (await authority.snapshot())["concurrency"]["in_flight"] == 1

    await gateway.release(admission)
    await gateway.release(admission)
    assert (await authority.snapshot())["concurrency"]["in_flight"] == 0


async def test_abandoned_client_lease_expires_and_capacity_recovers(tmp_path) -> None:
    clock = Clock(10.0)
    authority = store(
        tmp_path,
        maximum=1,
        lease=0.1,
        wall_clock=clock,
        monotonic_clock=clock,
    )
    first = await authority.acquire(ACCOUNT_A, uuid.uuid4())
    assert first.allowed
    clock.value = 10.05
    assert not (await authority.acquire(ACCOUNT_A, uuid.uuid4())).allowed

    clock.value = 10.11
    recovered = await authority.acquire(ACCOUNT_A, uuid.uuid4())
    assert recovered.allowed
    state = await authority.snapshot()
    assert state["concurrency"]["expired_leases"] == 1


async def test_coordinator_state_survives_restart(tmp_path) -> None:
    path = tmp_path / "private" / "limits.db"
    wall = Clock(100.0)
    monotonic = Clock(50.0)
    first = LimitCoordinatorStore(
        path=str(path),
        rate=RateLimit(60, 1),
        maximum=1,
        lease_seconds=30,
        wall_clock=wall,
        monotonic_clock=monotonic,
    )
    decision = await first.acquire(ACCOUNT_A, uuid.uuid4())
    first.close()

    # A wildly corrected wall clock cannot expire a request that may still be running. Restart
    # conservatively grants the persisted lease one fresh interval and no offline rate refill.
    wall.value = 10_000.0
    monotonic.value = 1.0
    restarted = LimitCoordinatorStore(
        path=str(path),
        rate=RateLimit(60, 1),
        maximum=1,
        lease_seconds=30,
        wall_clock=wall,
        monotonic_clock=monotonic,
    )
    assert not (await restarted.acquire(ACCOUNT_A, uuid.uuid4())).allowed
    state = await restarted.snapshot()
    assert state["concurrency"]["in_flight"] == 1
    assert state["rate_limit"]["allowed"] == 1
    assert decision.lease_id is not None
    assert await restarted.release(decision.lease_id)


async def test_coordinator_routes_require_the_private_token(tmp_path) -> None:
    authority = store(tmp_path, maximum=1)
    transport = httpx.ASGITransport(app=app_for(authority))
    async with httpx.AsyncClient(transport=transport, base_url="http://limits.test") as client:
        denied = await client.post(
            "/limits/admit",
            json={"account_id": str(ACCOUNT_A), "request_id": str(uuid.uuid4())},
        )
        assert denied.status_code == 401

        accepted = await client.post(
            "/limits/admit",
            json={"account_id": str(ACCOUNT_A), "request_id": str(uuid.uuid4())},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["allowed"] is True


async def test_gateway_fails_closed_before_model_work_when_coordinator_is_down(
    control_plane, upstream: UpstreamStub, signing_key: SigningKey
) -> None:
    settings = make_settings(
        rate_limit_requests_per_minute=60,
        rate_limit_burst=1,
        limit_coordinator_url="http://limits.invalid",
        limit_coordinator_token=TOKEN,
    )
    plane = DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=control_plane.client()),
        registry=make_registry(),
        client=upstream.client(),
    )
    plane.limits._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
    )
    transport = httpx.ASGITransport(app=create_inference_app(plane))
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "launch-model", "messages": []},
            headers={"Authorization": f"Bearer {signing_key.issue()}"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "limit_coordinator_unavailable"
        assert len(upstream.requests) == 0
        assert (await client.get("/readyz")).status_code == 503



async def test_two_independent_data_planes_enforce_one_shared_rate_limit(
    tmp_path,
    control_plane,
    upstream: UpstreamStub,
    signing_key: SigningKey,
) -> None:
    authority = store(tmp_path, rate=60, burst=1)
    coordinator_app = app_for(authority)

    planes: list[DataPlane] = []
    clients: list[httpx.AsyncClient] = []
    for _ in range(2):
        settings = make_settings(
            rate_limit_requests_per_minute=60,
            rate_limit_burst=1,
            limit_coordinator_url="http://limits.test",
            limit_coordinator_token=TOKEN,
        )
        plane = DataPlane(
            settings=settings,
            keys=KeyCache(settings, client=control_plane.client()),
            registry=make_registry(),
            client=upstream.client(),
        )
        plane.limits._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=coordinator_app),
            base_url="http://limits.test",
        )
        planes.append(plane)
        clients.append(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_inference_app(plane)),
                base_url="http://ingress.test",
            )
        )

    request = {
        "json": {"model": "launch-model", "messages": []},
        "headers": {"Authorization": f"Bearer {signing_key.issue()}"},
    }
    first = await clients[0].post("/v1/chat/completions", **request)
    second = await clients[1].post("/v1/chat/completions", **request)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"
    assert len(upstream.requests) == 1
    for client in clients:
        await client.aclose()



async def test_rate_is_spent_before_a_concurrency_refusal(tmp_path) -> None:
    clock = Clock(100.0)
    authority = store(
        tmp_path,
        rate=60,
        burst=2,
        maximum=1,
        wall_clock=clock,
        monotonic_clock=clock,
    )
    first = await authority.acquire(ACCOUNT_A, uuid.uuid4())
    assert first.allowed

    concurrency_refused = await authority.acquire(ACCOUNT_A, uuid.uuid4())
    assert concurrency_refused.reason == "too_many_in_flight"
    # The second request passed the rate bucket before losing the concurrency race, preserving the
    # historical order. A third request therefore hits rate, not concurrency.
    rate_refused = await authority.acquire(ACCOUNT_A, uuid.uuid4())
    assert rate_refused.reason == "rate_limited"



async def test_wall_clock_corrections_do_not_refill_or_expire_live_state(tmp_path) -> None:
    wall = Clock(100.0)
    monotonic = Clock(10.0)
    authority = store(
        tmp_path,
        rate=60,
        burst=1,
        maximum=1,
        lease=30,
        wall_clock=wall,
        monotonic_clock=monotonic,
    )
    assert (await authority.acquire(ACCOUNT_A, uuid.uuid4())).allowed

    wall.value = 100_000.0
    # No monotonic time passed: neither token nor concurrency slot may recover.
    refused = await authority.acquire(ACCOUNT_A, uuid.uuid4())
    assert refused.reason == "rate_limited"
    assert (await authority.snapshot())["concurrency"]["in_flight"] == 1

    wall.value = 1.0
    refused_after_rollback = await authority.acquire(ACCOUNT_A, uuid.uuid4())
    assert refused_after_rollback.reason == "rate_limited"


async def test_rate_policy_change_resets_at_an_explicit_restart_boundary(tmp_path) -> None:
    path = tmp_path / "private" / "limits.db"
    wall = Clock(100.0)
    monotonic = Clock(10.0)
    old = LimitCoordinatorStore(
        path=str(path),
        rate=RateLimit(1, 1),
        maximum=0,
        lease_seconds=30,
        wall_clock=wall,
        monotonic_clock=monotonic,
    )
    assert (await old.acquire(ACCOUNT_A, uuid.uuid4())).allowed
    old.close()

    # The new policy starts now with its declared burst. It is not applied retroactively to the
    # persisted interval under the old refill rate.
    wall.value = 159.0
    monotonic.value = 0.0
    changed = LimitCoordinatorStore(
        path=str(path),
        rate=RateLimit(60, 2),
        maximum=0,
        lease_seconds=30,
        wall_clock=wall,
        monotonic_clock=monotonic,
    )
    assert (await changed.acquire(ACCOUNT_A, uuid.uuid4())).allowed
    assert (await changed.acquire(ACCOUNT_A, uuid.uuid4())).allowed
    assert not (await changed.acquire(ACCOUNT_A, uuid.uuid4())).allowed



class LostFirstResponseTransport(httpx.AsyncBaseTransport):
    """Let the coordinator commit, then lose exactly the first response."""

    def __init__(self, app) -> None:
        self.inner = httpx.ASGITransport(app=app)
        self.requests: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        response = await self.inner.handle_async_request(request)
        if len(self.requests) == 1:
            raise httpx.ReadTimeout("response lost after commit", request=request)
        return response


async def test_ambiguous_admission_retries_the_same_request_identity(tmp_path) -> None:
    authority = store(tmp_path, rate=60, burst=1, maximum=1)
    app = app_for(authority)
    transport = LostFirstResponseTransport(app)
    gateway = SharedLimitManager(
        base_url="http://limits.test",
        token=TOKEN,
        timeout=1.0,
        renew_seconds=0.1,
        rate_enabled=True,
        concurrency_enabled=True,
        client=httpx.AsyncClient(transport=transport, base_url="http://limits.test"),
    )

    admission = await gateway.acquire(ACCOUNT_A)

    assert admission.allowed
    assert len(transport.requests) == 2
    assert transport.requests[0]["request_id"] == transport.requests[1]["request_id"]
    state = await authority.snapshot()
    assert state["rate_limit"]["allowed"] == 1
    assert state["concurrency"]["in_flight"] == 1
    await gateway.release(admission)


async def test_malformed_admission_is_sticky_unhealthy_until_a_valid_decision() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/limits/admit":
            return httpx.Response(200, json={})
        if request.url.path == "/limits/state":
            return httpx.Response(
                200,
                json={
                    "mode": "coordinator",
                    "healthy": True,
                    "rate_limit": {},
                    "concurrency": {},
                },
            )
        return httpx.Response(404)

    gateway = SharedLimitManager(
        base_url="http://limits.test",
        token=TOKEN,
        timeout=1.0,
        renew_seconds=1.0,
        rate_enabled=True,
        concurrency_enabled=False,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(LimitCoordinatorUnavailable):
        await gateway.acquire(ACCOUNT_A)
    state = await gateway.snapshot()
    assert state["healthy"] is False
    assert "omitted allowed" in state["last_error"]


async def test_malformed_state_fails_readiness_closed_instead_of_raising() -> None:
    gateway = SharedLimitManager(
        base_url="http://limits.test",
        token=TOKEN,
        timeout=1.0,
        renew_seconds=1.0,
        rate_enabled=True,
        concurrency_enabled=False,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
        ),
    )

    state = await gateway.snapshot()

    assert state["healthy"] is False
    assert state["mode"] == "shared"



def shared_plane(control_plane, upstream_client, coordinator_app) -> DataPlane:
    settings = make_settings(
        max_in_flight_per_account=1,
        limit_coordinator_url="http://limits.test",
        limit_coordinator_token=TOKEN,
    )
    plane = DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=control_plane.client()),
        registry=make_registry(),
        client=upstream_client,
    )
    plane.limits._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=coordinator_app),
        base_url="http://limits.test",
    )
    return plane


@pytest.mark.parametrize("streaming", [False, True])
async def test_full_gateway_releases_shared_concurrency_after_completion(
    tmp_path,
    control_plane,
    upstream: UpstreamStub,
    signing_key: SigningKey,
    streaming: bool,
) -> None:
    authority = store(tmp_path, maximum=1)
    plane = shared_plane(control_plane, upstream.client(), app_for(authority))
    transport = httpx.ASGITransport(app=create_inference_app(plane))
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "launch-model", "messages": [], "stream": streaming},
            headers={"Authorization": f"Bearer {signing_key.issue()}"},
        )
        assert response.status_code == 200
    assert (await authority.snapshot())["concurrency"]["in_flight"] == 0


async def test_full_gateway_releases_shared_concurrency_after_upstream_failure(
    tmp_path,
    control_plane,
    signing_key: SigningKey,
) -> None:
    authority = store(tmp_path, maximum=1)

    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("host down", request=request)

    plane = shared_plane(
        control_plane,
        httpx.AsyncClient(transport=httpx.MockTransport(failing)),
        app_for(authority),
    )
    transport = httpx.ASGITransport(app=create_inference_app(plane))
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "launch-model", "messages": []},
            headers={"Authorization": f"Bearer {signing_key.issue()}"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "upstream_unavailable"
    assert (await authority.snapshot())["concurrency"]["in_flight"] == 0



class BlockingReleaseTransport(httpx.AsyncBaseTransport):
    def __init__(self, app) -> None:
        self.inner = httpx.ASGITransport(app=app)
        self.release_started = asyncio.Event()
        self.allow_release = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/limits/release":
            self.release_started.set()
            await self.allow_release.wait()
        return await self.inner.handle_async_request(request)


async def test_stream_cancellation_during_shared_release_finishes_cleanup_once(
    tmp_path,
    control_plane,
    upstream: UpstreamStub,
    signing_key: SigningKey,
) -> None:
    from starlette.requests import Request

    from fabric_data_plane.app import _proxy

    authority = store(tmp_path, maximum=1)
    coordinator_app = app_for(authority)
    blocking = BlockingReleaseTransport(coordinator_app)
    plane = shared_plane(control_plane, upstream.client(), coordinator_app)
    plane.limits._client = httpx.AsyncClient(
        transport=blocking,
        base_url="http://limits.test",
    )
    body = json.dumps({"model": "launch-model", "stream": True}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [
            (b"authorization", f"Bearer {signing_key.issue()}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("ingress.test", 80),
    }
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    response = await _proxy(plane, Request(scope, receive), "/v1/chat/completions")

    async def consume() -> bytes:
        return b"".join([chunk async for chunk in response.body_iterator])

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(blocking.release_started.wait(), timeout=1)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    outer_cleanup = asyncio.create_task(response._cleanup())
    blocking.allow_release.set()
    await outer_cleanup
    await response._cleanup()

    assert (await authority.snapshot())["concurrency"]["in_flight"] == 0
    assert not plane._admitted_streams
    assert len(plane.usage.drain()) == 1



async def test_concurrency_only_change_preserves_rate_tokens(tmp_path) -> None:
    path = tmp_path / "private" / "limits.db"
    wall = Clock(100.0)
    monotonic = Clock(10.0)
    old = LimitCoordinatorStore(
        path=str(path),
        rate=RateLimit(1, 1),
        maximum=1,
        lease_seconds=30,
        wall_clock=wall,
        monotonic_clock=monotonic,
    )
    first = await old.acquire(ACCOUNT_A, uuid.uuid4())
    assert first.allowed and first.lease_id is not None
    await old.release(first.lease_id)
    old.close()

    wall.value = 101.0
    monotonic.value = 0.0
    changed = LimitCoordinatorStore(
        path=str(path),
        rate=RateLimit(1, 1),
        maximum=2,
        lease_seconds=30,
        wall_clock=wall,
        monotonic_clock=monotonic,
    )
    refused = await changed.acquire(ACCOUNT_A, uuid.uuid4())
    assert refused.reason == "rate_limited"
    assert (await changed.snapshot())["concurrency"]["max_in_flight_per_account"] == 2



class ToggleMalformedAdmissionTransport(httpx.AsyncBaseTransport):
    def __init__(self, app) -> None:
        self.inner = httpx.ASGITransport(app=app)
        self.malformed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.malformed and request.url.path == "/limits/admit":
            return httpx.Response(200, json={}, request=request)
        return await self.inner.handle_async_request(request)


async def test_valid_renewal_cannot_hide_a_broken_admission_protocol(tmp_path) -> None:
    authority = store(tmp_path, maximum=2, lease=0.5)
    transport = ToggleMalformedAdmissionTransport(app_for(authority))
    gateway = SharedLimitManager(
        base_url="http://limits.test",
        token=TOKEN,
        timeout=1.0,
        renew_seconds=0.05,
        rate_enabled=False,
        concurrency_enabled=True,
        client=httpx.AsyncClient(transport=transport, base_url="http://limits.test"),
    )
    active = await gateway.acquire(ACCOUNT_A)
    assert active.allowed

    transport.malformed = True
    with pytest.raises(LimitCoordinatorUnavailable):
        await gateway.acquire(ACCOUNT_A)
    await asyncio.sleep(0.12)  # the older lease renews successfully in the background
    assert (await gateway.snapshot())["healthy"] is False

    transport.malformed = False
    recovered = await gateway.acquire(ACCOUNT_A)
    assert recovered.allowed
    assert (await gateway.snapshot())["healthy"] is True
    await gateway.release(recovered)
    await gateway.release(active)



async def test_admission_http_status_failure_remains_sticky_across_state_probe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/limits/admit":
            return httpx.Response(404, json={"error": "missing"})
        if request.url.path == "/limits/state":
            return httpx.Response(
                200,
                json={
                    "mode": "coordinator",
                    "healthy": True,
                    "rate_limit": {},
                    "concurrency": {},
                },
            )
        return httpx.Response(404)

    gateway = SharedLimitManager(
        base_url="http://limits.test",
        token=TOKEN,
        timeout=1.0,
        renew_seconds=1.0,
        rate_enabled=True,
        concurrency_enabled=False,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(LimitCoordinatorUnavailable):
        await gateway.acquire(ACCOUNT_A)
    state = await gateway.snapshot()
    assert state["healthy"] is False
    assert "404" in state["last_error"]
