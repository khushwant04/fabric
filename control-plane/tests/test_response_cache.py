"""Tenant isolation and outage behavior for durable account response generations."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select

from app.core.cache import ResponseCache, commit_and_invalidate, get_cache
from app.core.config import Settings
from app.core.tenancy import declare_account, declare_system
from app.models import Account, DeploymentPlacement
from tests.helpers import bearer, create_deployment, enroll_stamp, onboard
from tests.test_metrics import report as metrics_report
from tests.test_stamps import _enroll_managed_stamp
from tests.test_telemetry import place, record


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.online = True
        self.ttls = {}

    async def get(self, key):
        if not self.online:
            raise RedisConnectionError("cache unavailable")
        return self.values.get(key)

    async def set(self, key, payload, *, ex):
        if not self.online:
            raise RedisConnectionError("cache unavailable")
        self.values[key] = payload
        self.ttls[key] = ex


@pytest.mark.asyncio
async def test_cache_keys_isolate_accounts_generations_and_route_parameters():
    redis = MemoryRedis()
    cache = ResponseCache(redis)
    account_a, account_b = uuid.uuid4(), uuid.uuid4()
    calls = []

    async def load(account, generation):
        calls.append((account, generation))
        return {"account": str(account), "generation": generation}

    async def read(account, generation, parameters=()):
        return await cache.get_or_load(
            account_id=account, version=generation, resource="deployment",
            parameters=parameters, response_type=dict[str, str | int],
            loader=lambda: load(account, generation),
        )

    first = await read(account_a, 1, ("deployment-a",))
    assert await read(account_a, 1, ("deployment-a",)) == first
    assert len(calls) == 1
    assert (await read(account_b, 1, ("deployment-a",)))["account"] == str(account_b)
    assert (await read(account_a, 2, ("deployment-a",)))["generation"] == 2
    await read(account_a, 2, ("deployment-b",))
    assert len(calls) == 4


async def test_redis_timeout_is_bounded_and_uses_authoritative_loader_during_cooldown():
    class HangingRedis:
        reads = 0

        async def get(self, key):
            self.reads += 1
            await asyncio.Event().wait()

        async def set(self, key, payload, *, ex):
            raise AssertionError("a failed read must skip the write during cooldown")

    redis = HangingRedis()
    cache = ResponseCache(redis, timeout_seconds=0.02)
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        return [calls]

    before = time.monotonic()
    for _ in range(2):
        await cache.get_or_load(
            account_id=uuid.uuid4(), version=1, resource="bounded",
            response_type=list[int], loader=load,
        )
    assert time.monotonic() - before < 0.5
    assert redis.reads == 1
    assert calls == 2


async def test_live_reads_use_short_ttl_and_large_values_are_not_cached():
    redis = MemoryRedis()
    cache = ResponseCache(redis, max_value_bytes=20)
    account_id = uuid.uuid4()

    async def small():
        return [1]

    async def large():
        return ["a" * 40]

    await cache.get_or_load(
        account_id=account_id, version=1, resource="status", response_type=list[int],
        loader=small, live=True,
    )
    assert redis.ttls[cache.key(account_id, 1, "status")] == 3
    assert await cache.get_or_load(
        account_id=account_id, version=1, resource="large", response_type=list[str], loader=large,
    ) == ["a" * 40]
    assert cache.key(account_id, 1, "large") not in redis.values


async def test_configured_redis_bounds_connections_and_disables_retries():
    cache = ResponseCache.from_settings(Settings(
        redis_url="redis://localhost:6379/0", api_cache_timeout_seconds=0.1,
        api_cache_max_connections=4,
    ))
    pool = cache._client.connection_pool
    assert pool.max_connections == 4
    assert pool.connection_kwargs["socket_timeout"] == 0.1
    assert pool.connection_kwargs["socket_connect_timeout"] == 0.1
    assert pool.connection_kwargs["retry"]._retries == 0
    await cache.close()


async def test_cached_deployment_status_usage_and_stamp_updates_are_immediate(client):
    redis = MemoryRedis()
    first_replica = ResponseCache(redis)
    second_replica = ResponseCache(redis)
    app = client._transport.app
    app.dependency_overrides[get_cache] = lambda: first_replica
    account_id, token = await onboard(client, "cache-status", "cache-status-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    stamp_id = enrolled["stamp"]["id"]
    await place(client, account_id, token, deployment["id"], stamp_id)
    base = f"/v1/accounts/{account_id}/deployments/{deployment['id']}"
    headers = bearer(token)
    assert (await client.get(f"{base}/status", headers=headers)).json() == []
    assert (await client.get(f"{base}/usage", headers=headers)).json()["events"] == 0
    assert (await client.get(base, headers=headers)).json()["status"] == "pending"
    assert (await client.get(f"{base}/placements", headers=headers)).json()[0]["status"] != "ready"
    stamps_url = f"/v1/accounts/{account_id}/stamps"
    assert (await client.get(stamps_url, headers=headers)).json()[0]["status"] == "registered"

    # Independent CP instances share Redis but read a durable database generation.
    app.dependency_overrides[get_cache] = lambda: second_replica
    report = await client.post(
        f"/v1/stamps/{stamp_id}/status", headers=bearer(enrolled["agent_credential"]),
        json={"deployment_id": deployment["id"], "observed_generation": 1,
              "phase": "ready", "ready_replicas": 1, "unavailable_replicas": 0,
              "endpoint": "https://inference.example.test/v1", "conditions": []},
    )
    assert report.status_code == 200, report.text
    assert (await client.get(f"{base}/status", headers=headers)).json()[0]["ready_replicas"] == 1
    assert (await client.get(base, headers=headers)).json()["status"] == "ready"
    assert (await client.get(f"{base}/placements", headers=headers)).json()[0]["status"] == "ready"
    ingested = await client.post(
        "/v1/telemetry/usage", headers=bearer(enrolled["telemetry_credential"]),
        json={"records": [record(deployment["id"], "cached-usage")]},
    )
    assert ingested.status_code == 200, ingested.text
    assert (await client.get(f"{base}/usage", headers=headers)).json()["events"] == 1
    assert (await client.get(f"/v1/accounts/{account_id}/deployments/usage",
                             headers=headers)).json()["events"] == 1
    heartbeat = await client.post(
        f"/v1/stamps/{stamp_id}/heartbeat", headers=bearer(enrolled["agent_credential"]), json={},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert (await client.get(stamps_url, headers=headers)).json()[0]["status"] == "active"

    foreign_account, foreign_token = await onboard(client, "cache-other", "cache-other-account")
    # A populated cache does not bypass account binding or deployment ownership.
    assert (await client.get(base, headers=bearer(foreign_token))).status_code == 403
    foreign_path = base.replace(account_id, foreign_account)
    assert (await client.get(foreign_path, headers=bearer(foreign_token))).status_code == 404
    assert (await client.get(base)).status_code == 401

    deleted = await client.delete(base, headers=headers)
    assert deleted.status_code == 204
    assert (await client.get(base, headers=headers)).status_code == 404


async def test_managed_stamp_invalidates_customer_cache_not_only_system_account(client, db_session):
    from sqlalchemy import update

    redis = MemoryRedis()
    app = client._transport.app
    app.dependency_overrides[get_cache] = lambda: ResponseCache(redis)
    account_id, token = await onboard(client, "cache-managed", "cache-managed-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled, system = await _enroll_managed_stamp(client, db_session)
    system_id = system.id
    await db_session.execute(
        update(Account).where(Account.id == uuid.UUID(account_id))
        .values(managed_capacity_enabled=True)
    )
    await db_session.commit()
    stamp_id = enrolled["stamp"]["id"]
    await place(client, account_id, token, deployment["id"], stamp_id)
    base = f"/v1/accounts/{account_id}/deployments/{deployment['id']}"
    headers = bearer(token)
    assert (await client.get(base, headers=headers)).json()["status"] == "pending"
    assert (await client.get(f"{base}/usage", headers=headers)).json()["events"] == 0
    await declare_system(db_session)
    placement = (await db_session.execute(
        select(DeploymentPlacement)
        .where(DeploymentPlacement.deployment_id == uuid.UUID(deployment["id"]))
    )).scalar_one()
    generation = placement.desired_generation
    await db_session.rollback()

    response = await client.post(
        f"/v1/stamps/{stamp_id}/status", headers=bearer(enrolled["agent_credential"]),
        json={"deployment_id": deployment["id"], "observed_generation": generation,
              "phase": "ready", "ready_replicas": 1, "unavailable_replicas": 0,
              "endpoint": "https://inference.example.test/v1", "conditions": []},
    )
    assert response.status_code == 200, response.text
    assert (await client.get(base, headers=headers)).json()["status"] == "ready"
    response = await client.post(
        "/v1/telemetry/usage", headers=bearer(enrolled["telemetry_credential"]),
        json={"records": [record(deployment["id"], "cached-managed")]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 1
    assert (await client.get(f"{base}/usage", headers=headers)).json()["events"] == 1
    await declare_system(db_session)
    db_session.expire_all()
    system_current = (await db_session.execute(
        select(Account).where(Account.id == system_id)
    )).scalar_one()
    assert system_current.cache_version == 2  # Enrollment only; reports belong to the customer.


async def test_periodic_reports_keep_management_cache_warm_but_inventory_and_changes_are_fresh(
    client, db_session
):
    redis = MemoryRedis()
    cache = ResponseCache(redis)
    client._transport.app.dependency_overrides[get_cache] = lambda: cache
    account_id, token = await onboard(client, "cache-periodic", "cache-periodic-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    stamp_id = enrolled["stamp"]["id"]
    await place(client, account_id, token, deployment["id"], stamp_id)
    headers = bearer(token)
    base = f"/v1/accounts/{account_id}/deployments/{deployment['id']}"
    stamps_url = f"/v1/accounts/{account_id}/stamps"
    status_payload = {
        "deployment_id": deployment["id"], "observed_generation": 1,
        "phase": "ready", "ready_replicas": 1, "unavailable_replicas": 0,
        "endpoint": "https://inference.example.test/v1", "conditions": [],
    }
    agent_headers = bearer(enrolled["agent_credential"])
    reported = await client.post(f"/v1/stamps/{stamp_id}/status", headers=agent_headers,
                                 json=status_payload)
    assert reported.status_code == 200, reported.text
    assert (await client.get(base, headers=headers)).json()["status"] == "ready"
    assert (await client.get(stamps_url, headers=headers)).json()[0]["status"] == "registered"
    await declare_system(db_session)
    generation = (await db_session.execute(
        select(Account.cache_version).where(Account.id == uuid.UUID(account_id))
    )).scalar_one()
    await db_session.rollback()
    key = cache.key(uuid.UUID(account_id), generation, "deployment", deployment["id"])
    assert key in redis.values

    # Repeated identical status is liveness evidence, not a changed deployment.
    reported = await client.post(f"/v1/stamps/{stamp_id}/status", headers=agent_headers,
                                 json=status_payload)
    assert reported.status_code == 200, reported.text
    heartbeat = await client.post(
        f"/v1/stamps/{stamp_id}/heartbeat", headers=agent_headers, json={}
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert (await client.get(stamps_url, headers=headers)).json()[0]["status"] == "active"
    metrics = await client.post("/v1/telemetry/metrics", json=metrics_report(),
                                headers=bearer(enrolled["telemetry_credential"]))
    assert metrics.status_code == 200, metrics.text
    stamp = (await client.get(stamps_url, headers=headers)).json()[0]
    assert stamp["capabilities"]["metrics"]["gpu"]["devices"] == 1
    await declare_system(db_session)
    current = (await db_session.execute(
        select(Account.cache_version).where(Account.id == uuid.UUID(account_id))
    )).scalar_one()
    assert current == generation
    await db_session.rollback()
    assert (await client.get(base, headers=headers)).json()["status"] == "ready"
    assert key in redis.values

    # An actual availability transition invalidates immediately, even within the TTL.
    reported = await client.post(
        f"/v1/stamps/{stamp_id}/status", headers=agent_headers,
        json=status_payload | {"phase": "progressing", "ready_replicas": 0},
    )
    assert reported.status_code == 200, reported.text
    assert (await client.get(base, headers=headers)).json()["status"] == "progressing"


@pytest.mark.asyncio
async def test_cache_outage_and_invalid_cached_schema_fall_back_to_authoritative_loader():
    redis = MemoryRedis()
    cache = ResponseCache(redis)
    account = uuid.uuid4()
    current = [1]

    async def load():
        return current[:]

    async def read(version):
        return await cache.get_or_load(account_id=account, version=version,
                                       resource="values", response_type=list[int], loader=load)

    assert await read(1) == [1]
    redis.online = False
    current[:] = [2]
    # A durable version change while Redis is offline cannot make v1 current again.
    assert await read(2) == [2]
    redis.online = True
    cache._unavailable_until = 0
    assert await read(2) == [2]
    key = ResponseCache.key(account, 2, "values")
    redis.values[key] = json.dumps({"wrong": "schema"})
    current[:] = [3]
    assert await read(2) == [3]


@pytest.mark.asyncio
async def test_durable_invalidation_commits_mutation_and_advances_each_account_once(db_session):
    await declare_system(db_session)
    account = Account(slug="cache-account", name="Before", status="active")
    db_session.add(account)
    await db_session.commit()
    assert account.cache_version == 1
    account_id = account.id
    declare_account(account_id)
    account.name = "After"
    await commit_and_invalidate(db_session, [account.id, account.id])
    db_session.expire_all()
    refreshed = (
        await db_session.execute(select(Account).where(Account.id == account_id))
    ).scalar_one()
    assert refreshed.name == "After"
    assert refreshed.cache_version == 2


@pytest.mark.asyncio
async def test_missing_invalidation_anchor_cannot_commit_other_pending_changes(db_session):
    await declare_system(db_session)
    account = Account(slug="cache-rollback", name="Before", status="active")
    db_session.add(account)
    await db_session.commit()
    account_id = account.id
    declare_account(account_id)
    account.name = "Uncommitted"
    with pytest.raises(LookupError):
        await commit_and_invalidate(db_session, [uuid.uuid4()])
    await db_session.rollback()
    stored = (
        await db_session.execute(select(Account).where(Account.id == account_id))
    ).scalar_one()
    assert stored.name == "Before"
    assert stored.cache_version == 1
