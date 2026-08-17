"""Rate limiting and concurrency capping.

A stamp serves a fixed amount of GPU, and without limits one account can consume all of
it while the model host's queue makes everyone slow instead of refusing anyone.
"""

from __future__ import annotations

import uuid

import httpx

from fabric_data_plane.app import DataPlane, create_admin_app, create_inference_app
from fabric_data_plane.keys import KeyCache
from fabric_data_plane.limits import ConcurrencyLimiter, RateLimit, RateLimiter
from tests.conftest import (
    ACCOUNT_A,
    ACCOUNT_B,
    SigningKey,
    UpstreamStub,
    make_registry,
    make_settings,
)

ACCOUNT = uuid.UUID("33333333-3333-3333-3333-333333333333")


def test_a_new_account_starts_with_its_full_burst() -> None:
    """A first request must not be penalised for having no history."""
    limiter = RateLimiter(RateLimit(requests_per_minute=60, burst=3))
    assert [limiter.check(ACCOUNT, now=0.0) for _ in range(3)] == [None, None, None]


def test_the_allowance_is_refused_once_spent() -> None:
    limiter = RateLimiter(RateLimit(requests_per_minute=60, burst=2))
    limiter.check(ACCOUNT, now=0.0)
    limiter.check(ACCOUNT, now=0.0)

    wait = limiter.check(ACCOUNT, now=0.0)
    assert wait is not None
    # The caller is told when to return rather than left to guess.
    assert 0 < wait <= 1.0


def test_the_bucket_refills_continuously() -> None:
    """A fixed window would allow a double burst across its boundary."""
    limiter = RateLimiter(RateLimit(requests_per_minute=60, burst=1))
    assert limiter.check(ACCOUNT, now=0.0) is None
    assert limiter.check(ACCOUNT, now=0.5) is not None
    # One per second at 60/min, so a full token exists here.
    assert limiter.check(ACCOUNT, now=1.01) is None


def test_accounts_do_not_share_an_allowance() -> None:
    limiter = RateLimiter(RateLimit(requests_per_minute=60, burst=1))
    assert limiter.check(ACCOUNT_A, now=0.0) is None
    # Spending A's allowance must not affect B.
    assert limiter.check(ACCOUNT_B, now=0.0) is None
    assert limiter.check(ACCOUNT_A, now=0.0) is not None


def test_a_zero_rate_disables_the_limit() -> None:
    """The default: the data plane cannot know a host's capacity, so it does not guess."""
    limiter = RateLimiter(RateLimit(requests_per_minute=0, burst=0))
    assert not limiter.enabled
    assert all(limiter.check(ACCOUNT, now=float(i)) is None for i in range(100))


async def test_concurrency_is_capped_and_released() -> None:
    limiter = ConcurrencyLimiter(2)

    assert await limiter.acquire(ACCOUNT)
    assert await limiter.acquire(ACCOUNT)
    assert not await limiter.acquire(ACCOUNT)

    await limiter.release(ACCOUNT)
    # A finished request frees the slot it held.
    assert await limiter.acquire(ACCOUNT)


async def test_concurrency_is_per_account() -> None:
    limiter = ConcurrencyLimiter(1)
    assert await limiter.acquire(ACCOUNT_A)
    assert not await limiter.acquire(ACCOUNT_A)
    # One account saturating its share must not block another's.
    assert await limiter.acquire(ACCOUNT_B)


async def test_releasing_to_zero_forgets_the_account() -> None:
    """An account that stops sending should not occupy memory for the process's life."""
    limiter = ConcurrencyLimiter(1)
    await limiter.acquire(ACCOUNT)
    await limiter.release(ACCOUNT)
    assert limiter.snapshot()["in_flight"] == 0


def limited_plane(control_plane, upstream: UpstreamStub, **overrides) -> DataPlane:
    """A plane like the shared fixture's, with limits configured."""
    settings = make_settings(**overrides)
    return DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=control_plane.client()),
        registry=make_registry(),
        client=upstream.client(),
    )


async def test_an_over_rate_caller_is_refused_with_retry_after(
    control_plane, upstream: UpstreamStub, signing_key: SigningKey
) -> None:
    plane = limited_plane(
        control_plane, upstream, rate_limit_requests_per_minute=60, rate_limit_burst=1
    )
    transport = httpx.ASGITransport(app=create_inference_app(plane))

    async with httpx.AsyncClient(transport=transport, base_url="http://ingress") as client:
        headers = {"Authorization": f"Bearer {signing_key.issue()}"}
        body = {"model": "launch-model", "messages": [{"role": "user", "content": "hi"}]}

        first = await client.post("/v1/chat/completions", json=body, headers=headers)
        second = await client.post("/v1/chat/completions", json=body, headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"
    # Without this a client can only guess when to retry.
    assert second.headers["Retry-After"]


async def test_a_refused_request_never_reaches_the_model_host(
    control_plane, upstream: UpstreamStub, signing_key: SigningKey
) -> None:
    """Refusing early is the point: the GPU must not do work for a request over quota."""
    plane = limited_plane(
        control_plane, upstream, rate_limit_requests_per_minute=60, rate_limit_burst=1
    )
    transport = httpx.ASGITransport(app=create_inference_app(plane))

    async with httpx.AsyncClient(transport=transport, base_url="http://ingress") as client:
        headers = {"Authorization": f"Bearer {signing_key.issue()}"}
        body = {"model": "launch-model", "messages": [{"role": "user", "content": "hi"}]}
        await client.post("/v1/chat/completions", json=body, headers=headers)
        before = len(upstream.requests)
        await client.post("/v1/chat/completions", json=body, headers=headers)

    assert len(upstream.requests) == before


async def test_the_slot_is_released_when_the_upstream_fails(
    control_plane, signing_key: SigningKey
) -> None:
    """A leaked slot would permanently shrink the account's concurrency."""

    def failing(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("model host down")

    settings = make_settings(max_in_flight_per_account=1)
    plane = DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=control_plane.client()),
        registry=make_registry(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(failing)),
    )
    transport = httpx.ASGITransport(app=create_inference_app(plane))

    async with httpx.AsyncClient(transport=transport, base_url="http://ingress") as client:
        headers = {"Authorization": f"Bearer {signing_key.issue()}"}
        body = {"model": "launch-model", "messages": []}
        for _ in range(3):
            response = await client.post("/v1/chat/completions", json=body, headers=headers)
            # Every attempt fails upstream, but none is refused for concurrency, which
            # only holds if the slot came back each time.
            assert response.json()["error"]["code"] == "upstream_unavailable", response.text

    assert plane.concurrency.snapshot()["in_flight"] == 0


async def test_limit_state_is_visible_on_the_admin_listener(
    control_plane, upstream: UpstreamStub
) -> None:
    plane = limited_plane(
        control_plane, upstream, rate_limit_requests_per_minute=120, max_in_flight_per_account=4
    )
    transport = httpx.ASGITransport(app=create_admin_app(plane))

    async with httpx.AsyncClient(transport=transport, base_url="http://admin") as client:
        state = (await client.get("/admin/limits")).json()

    assert state["rate_limit"]["enabled"] is True
    assert state["rate_limit"]["requests_per_minute"] == 120
    assert state["concurrency"]["max_in_flight_per_account"] == 4
