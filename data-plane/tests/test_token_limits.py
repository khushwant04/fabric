"""Token-rate limits: accounts are charged the model tokens their requests consumed (ADR 0017)."""

from __future__ import annotations

import sqlite3
import time
import uuid

import httpx
import pytest

from fabric_data_plane.app import DataPlane, create_admin_app, create_inference_app
from fabric_data_plane.keys import KeyCache
from fabric_data_plane.limits import (
    ConcurrencyLimiter,
    RateLimit,
    RateLimiter,
    TokenRate,
    TokenRateLimiter,
)
from fabric_data_plane.shared_limits import LimitCoordinatorStore, LocalLimitManager
from tests.conftest import (
    ACCOUNT_A,
    ACCOUNT_B,
    SigningKey,
    UpstreamStub,
    make_registry,
    make_settings,
)
from tests.test_shared_limits import TOKEN, Clock, app_for, manager

RATE = TokenRate(tokens_per_minute=60, burst=10)  # one token per second


# --- in-process primitive ----------------------------------------------------------------


def test_a_new_account_may_start_with_its_burst_banked() -> None:
    limiter = TokenRateLimiter(RATE)
    assert limiter.check(ACCOUNT_A, now=0.0) is None


def test_debt_refuses_until_it_is_repaid() -> None:
    limiter = TokenRateLimiter(RATE)
    limiter.debit(ACCOUNT_A, 25, now=0.0)  # 10 banked - 25 = 15 in debt
    wait = limiter.check(ACCOUNT_A, now=0.0)
    assert wait == pytest.approx(15.0)
    assert limiter.check(ACCOUNT_A, now=14.0) is not None
    assert limiter.check(ACCOUNT_A, now=15.0) is None


def test_a_request_that_fits_never_puts_the_account_in_debt() -> None:
    limiter = TokenRateLimiter(RATE)
    limiter.debit(ACCOUNT_A, 10, now=0.0)
    assert limiter.check(ACCOUNT_A, now=0.0) is None


def test_banked_tokens_never_exceed_the_burst() -> None:
    limiter = TokenRateLimiter(RATE)
    # An hour idle banks only the burst, so the next big request still runs into debt.
    limiter.debit(ACCOUNT_A, 30, now=3600.0)
    assert limiter.check(ACCOUNT_A, now=3600.0) == pytest.approx(20.0)


def test_one_accounts_debt_is_not_anothers() -> None:
    limiter = TokenRateLimiter(RATE)
    limiter.debit(ACCOUNT_A, 100, now=0.0)
    assert limiter.check(ACCOUNT_A, now=0.0) is not None
    assert limiter.check(ACCOUNT_B, now=0.0) is None


def test_a_zero_rate_disables_the_limit() -> None:
    limiter = TokenRateLimiter(TokenRate(tokens_per_minute=0, burst=0))
    limiter.debit(ACCOUNT_A, 10**9)
    assert limiter.check(ACCOUNT_A) is None


async def test_an_account_in_debt_does_not_also_spend_its_request_rate() -> None:
    rate = RateLimiter(RateLimit(requests_per_minute=60, burst=5))
    tokens = TokenRateLimiter(RATE)
    limits = LocalLimitManager(rate, ConcurrencyLimiter(0), tokens)
    admitted = await limits.acquire(ACCOUNT_A)
    await limits.release(admitted, tokens=1000)

    refused = await limits.acquire(ACCOUNT_A)
    assert (refused.allowed, refused.reason) == (False, "token_rate_limited")
    assert refused.retry_after is not None and refused.retry_after > 0
    assert rate.snapshot()["allowed"] == 1


# --- shared coordinator ------------------------------------------------------------------


def token_store(tmp_path, *, rate: TokenRate = RATE, **clocks) -> LimitCoordinatorStore:
    return LimitCoordinatorStore(
        path=str(tmp_path / "private" / "limits.db"),
        rate=RateLimit(requests_per_minute=0, burst=0),
        maximum=0,
        lease_seconds=60.0,
        token_rate=rate,
        wall_clock=clocks.get("wall_clock", time.time),
        monotonic_clock=clocks.get("monotonic_clock", time.monotonic),
    )


async def test_the_coordinator_refuses_an_account_in_debt(tmp_path) -> None:
    authority = token_store(tmp_path, monotonic_clock=Clock(0.0))
    request = uuid.uuid4()
    assert (await authority.acquire(ACCOUNT_A, request)).allowed
    assert await authority.consume(request, ACCOUNT_A, 0) is False  # nothing to charge
    assert await authority.consume(request, ACCOUNT_A, 25) is True

    refused = await authority.acquire(ACCOUNT_A, uuid.uuid4())
    assert (refused.allowed, refused.reason) == (False, "token_rate_limited")
    assert refused.retry_after == pytest.approx(15.0)
    assert (await authority.acquire(ACCOUNT_B, uuid.uuid4())).allowed


async def test_a_debit_is_charged_once_however_often_it_is_sent(tmp_path) -> None:
    authority = token_store(tmp_path, monotonic_clock=Clock(0.0))
    request = uuid.uuid4()
    await authority.acquire(ACCOUNT_A, request)
    assert await authority.consume(request, ACCOUNT_A, 8) is True
    assert await authority.consume(request, ACCOUNT_A, 8) is False

    state = await authority.snapshot()
    assert state["token_rate"]["debited"] == 8
    # 10 banked - 8 once leaves the account solvent; charging twice would not.
    assert (await authority.acquire(ACCOUNT_A, uuid.uuid4())).allowed


async def test_a_debit_cannot_be_charged_to_another_account(tmp_path) -> None:
    authority = token_store(tmp_path)
    request = uuid.uuid4()
    await authority.acquire(ACCOUNT_A, request)
    with pytest.raises(ValueError):
        await authority.consume(request, ACCOUNT_B, 5)


async def test_a_debit_after_its_admission_expired_still_charges_once(tmp_path) -> None:
    clock = Clock(0.0)
    authority = token_store(tmp_path, monotonic_clock=clock)
    request = uuid.uuid4()
    await authority.acquire(ACCOUNT_A, request)
    clock.value = 120.0  # past the 60 s lease, so the admission row is purged
    assert await authority.consume(request, ACCOUNT_A, 30) is True
    assert await authority.consume(request, ACCOUNT_A, 30) is False
    assert (await authority.snapshot())["token_rate"]["debited"] == 30


async def test_debt_survives_a_coordinator_restart_without_refilling(tmp_path) -> None:
    wall = Clock(1_000.0)
    first = token_store(tmp_path, wall_clock=wall, monotonic_clock=Clock(0.0))
    request = uuid.uuid4()
    await first.acquire(ACCOUNT_A, request)
    await first.consume(request, ACCOUNT_A, 40)  # 30 in debt
    first.close()

    # An hour of downtime by the wall clock grants nothing that cannot be proven.
    wall.value += 3_600.0
    restarted = token_store(tmp_path, wall_clock=wall, monotonic_clock=Clock(500.0))
    refused = await restarted.acquire(ACCOUNT_A, uuid.uuid4())
    assert refused.reason == "token_rate_limited"
    assert refused.retry_after == pytest.approx(30.0)


async def test_a_new_token_policy_starts_every_account_at_its_burst(tmp_path) -> None:
    first = token_store(tmp_path, monotonic_clock=Clock(0.0))
    request = uuid.uuid4()
    await first.acquire(ACCOUNT_A, request)
    await first.consume(request, ACCOUNT_A, 1_000)
    first.close()

    changed = token_store(
        tmp_path, rate=TokenRate(tokens_per_minute=600, burst=100), monotonic_clock=Clock(0.0)
    )
    assert (await changed.acquire(ACCOUNT_A, uuid.uuid4())).allowed


def test_a_store_written_before_token_limits_is_upgraded_in_place(tmp_path) -> None:
    path = tmp_path / "private" / "limits.db"
    path.parent.mkdir(mode=0o700)
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE admissions (
            request_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, allowed INTEGER NOT NULL,
            reason TEXT, retry_after REAL, lease_id TEXT, expires_at REAL NOT NULL,
            released INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE limit_policy (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            requests_per_minute INTEGER NOT NULL, burst INTEGER NOT NULL,
            maximum INTEGER NOT NULL
        );
        INSERT INTO limit_policy VALUES (1, 0, 0, 0);
        """
    )
    legacy.close()

    upgraded = token_store(tmp_path)
    columns = {row[1] for row in upgraded._database.execute("PRAGMA table_info(admissions)")}
    assert "consumed" in columns
    policy = upgraded._database.execute(
        "SELECT tokens_per_minute, token_burst FROM limit_policy"
    ).fetchone()
    assert tuple(policy) == (60, 10)


async def test_gateways_share_one_token_bucket_through_the_coordinator(tmp_path) -> None:
    authority = token_store(tmp_path)
    app = app_for(authority)
    first = manager(app)
    first.token_rate_enabled = True
    second = manager(app)
    second.token_rate_enabled = True

    admitted = await first.acquire(ACCOUNT_A)
    assert admitted.allowed and admitted.request_id is not None
    await first.release(admitted, tokens=50)

    refused = await second.acquire(ACCOUNT_A)
    assert (refused.allowed, refused.reason) == (False, "token_rate_limited")
    assert (await authority.snapshot())["token_rate"]["debited"] == 50


async def test_the_consume_endpoint_rejects_malformed_counts(tmp_path) -> None:
    app = app_for(token_store(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://limits.test"
    ) as client:
        for tokens in (-1, 1.5, True, "9"):
            response = await client.post(
                "/limits/consume",
                json={"request_id": str(uuid.uuid4()), "account_id": str(ACCOUNT_A),
                      "tokens": tokens},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert response.status_code == 400, tokens
            assert response.json()["error"]["code"] == "invalid_token_count"


# --- through the gateway -----------------------------------------------------------------


def token_limited_plane(control_plane, upstream: UpstreamStub) -> DataPlane:
    settings = make_settings(
        token_rate_limit_tokens_per_minute=60, token_rate_limit_burst=10
    )
    return DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=control_plane.client()),
        registry=make_registry(),
        client=upstream.client(),
    )


async def test_metered_tokens_are_charged_and_then_refused(
    control_plane, upstream: UpstreamStub, signing_key: SigningKey
) -> None:
    plane = token_limited_plane(control_plane, upstream)
    transport = httpx.ASGITransport(app=create_inference_app(plane))
    body = {"model": "launch-model", "messages": [{"role": "user", "content": "hi"}]}
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress") as client:
        headers = {"Authorization": f"Bearer {signing_key.issue()}"}
        # The stub reports 11 + 7 = 18 tokens, so one request takes 10 banked to -8.
        first = await client.post("/v1/chat/completions", json=body, headers=headers)
        before = len(upstream.requests)
        second = await client.post("/v1/chat/completions", json=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "token_rate_limited"
    # Eight tokens of debt at one per second.
    assert int(second.headers["Retry-After"]) >= 8
    assert len(upstream.requests) == before
    assert plane.token_limiter.snapshot()["debited"] == 18


async def test_streamed_tokens_are_charged_too(
    control_plane, upstream: UpstreamStub, signing_key: SigningKey
) -> None:
    plane = token_limited_plane(control_plane, upstream)
    transport = httpx.ASGITransport(app=create_inference_app(plane))
    body = {"model": "launch-model", "messages": [], "stream": True}
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress") as client:
        headers = {"Authorization": f"Bearer {signing_key.issue()}"}
        streamed = await client.post("/v1/chat/completions", json=body, headers=headers)
        assert streamed.status_code == 200
        refused = await client.post("/v1/chat/completions", json=body, headers=headers)

    # The host reported 11 + 7 = 18 for the stream: 10 banked -> -8.
    assert plane.token_limiter.snapshot()["debited"] == 18
    assert refused.json()["error"]["code"] == "token_rate_limited"


async def test_token_limit_state_is_visible_on_the_admin_listener(
    control_plane, upstream: UpstreamStub
) -> None:
    plane = token_limited_plane(control_plane, upstream)
    transport = httpx.ASGITransport(app=create_admin_app(plane))
    async with httpx.AsyncClient(transport=transport, base_url="http://admin") as client:
        state = (await client.get("/admin/limits")).json()
    assert state["token_rate"] == {
        "enabled": True,
        "tokens_per_minute": 60,
        "burst": 10,
        "accounts_tracked": 0,
        "debited": 0,
        "rejected": 0,
    }


async def test_a_stream_without_a_usage_report_is_still_charged_an_estimate(
    control_plane, upstream: UpstreamStub, signing_key: SigningKey
) -> None:
    """Otherwise hanging up before the final report would avoid the limit indefinitely."""
    upstream.report_stream_usage = False
    plane = token_limited_plane(control_plane, upstream)
    transport = httpx.ASGITransport(app=create_inference_app(plane))
    prompt = "x" * 200  # about 50 tokens by the routing estimate
    body = {
        "model": "launch-model",
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": 100_000,  # an unspent output budget is not charged
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress") as client:
        headers = {"Authorization": f"Bearer {signing_key.issue()}"}
        streamed = await client.post("/v1/chat/completions", json=body, headers=headers)
        assert streamed.status_code == 200
        refused = await client.post("/v1/chat/completions", json=body, headers=headers)

    debited = plane.token_limiter.snapshot()["debited"]
    assert 50 <= debited < 1_000
    assert refused.json()["error"]["code"] == "token_rate_limited"
    # Billing is unchanged: nothing is recorded without a report.
    assert plane.usage.drain() == []
