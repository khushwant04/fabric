"""Tenant isolation and outage behavior for durable account response generations."""

from __future__ import annotations

import json
import uuid

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select

from app.core.cache import ResponseCache, commit_and_invalidate
from app.core.tenancy import declare_account, declare_system
from app.models import Account


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.online = True

    async def get(self, key):
        if not self.online:
            raise RedisConnectionError("cache unavailable")
        return self.values.get(key)

    async def set(self, key, payload, *, ex):
        if not self.online:
            raise RedisConnectionError("cache unavailable")
        assert ex == 60
        self.values[key] = payload


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
