"""Optional, account-scoped response caching.

PostgreSQL remains authoritative.  The cache generation is stored on the account and
advanced in the same transaction as mutations, so a Redis outage cannot make an old
response current again when Redis recovers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

from pydantic import TypeAdapter, ValidationError
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Account

logger = logging.getLogger("fabric.control_plane.cache")
T = TypeVar("T")


class ResponseCache:
    """Small fail-open wrapper around Redis for validated response objects."""

    def __init__(
        self,
        client: Redis | None,
        ttl_seconds: int = 60,
        *,
        live_ttl_seconds: int = 3,
        timeout_seconds: float = 0.2,
        max_value_bytes: int = 1048576,
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._live_ttl_seconds = live_ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._max_value_bytes = max_value_bytes
        self._unavailable_until = 0.0

    @classmethod
    def from_settings(cls, settings: Settings) -> ResponseCache:
        if settings.redis_url is None:
            return cls(None, settings.api_cache_ttl_seconds)
        return cls(
            Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=settings.api_cache_timeout_seconds,
                socket_timeout=settings.api_cache_timeout_seconds,
                retry=Retry(NoBackoff(), 0),
                retry_on_timeout=False,
                max_connections=settings.api_cache_max_connections,
            ),
            settings.api_cache_ttl_seconds,
            live_ttl_seconds=settings.api_cache_live_ttl_seconds,
            timeout_seconds=settings.api_cache_timeout_seconds,
            max_value_bytes=settings.api_cache_max_value_bytes,
        )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def _available(self) -> bool:
        return self._client is not None and time.monotonic() >= self._unavailable_until

    def _unavailable(self, operation: str, error: Exception) -> None:
        # A failed dependency must not add its timeout to every dashboard request.
        # Versions still come from PostgreSQL during this brief retry cooldown.
        self._unavailable_until = time.monotonic() + 5
        logger.warning("response cache %s unavailable error=%s", operation, type(error).__name__)

    @staticmethod
    def key(account_id: uuid.UUID, version: int, resource: str, *parameters: object) -> str:
        """Build a tenant-visible key from non-secret, canonical route parameters."""
        suffix = ":".join(str(value) for value in parameters)
        base = f"fabric:cp:v1:account:{account_id}:v:{version}:{resource}"
        return f"{base}:{suffix}" if suffix else base

    async def get_or_load(
        self,
        *,
        account_id: uuid.UUID,
        version: int,
        resource: str,
        response_type: object,
        loader: Callable[[], Awaitable[T]],
        parameters: tuple[object, ...] = (),
        live: bool = False,
    ) -> T:
        adapter = TypeAdapter(response_type)
        key = self.key(account_id, version, resource, *parameters)
        if self._available():
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    cached = await self._client.get(key)
                if cached is not None:
                    if len(cached.encode("utf-8")) > self._max_value_bytes:
                        raise ValueError("cached response exceeds size limit")
                    return adapter.validate_json(cached)
            except (RedisError, TimeoutError, OSError) as error:
                self._unavailable("read", error)
            except (ValidationError, ValueError, TypeError):
                logger.warning("response cache contains invalid response key=%s", key)

        value = await loader()
        validated = adapter.validate_python(value)
        if self._available():
            try:
                payload = json.dumps(
                    adapter.dump_python(validated, mode="json"),
                    separators=(",", ":"),
                )
                if len(payload.encode("utf-8")) <= self._max_value_bytes:
                    async with asyncio.timeout(self._timeout_seconds):
                        await self._client.set(
                            key, payload, ex=self._live_ttl_seconds if live else self._ttl_seconds
                        )
            except (RedisError, TimeoutError, OSError) as error:
                self._unavailable("write", error)
            except (ValidationError, ValueError, TypeError):
                logger.warning("response cache could not serialize response key=%s", key)
        return validated

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


_cache = ResponseCache(None)


def configure_cache(settings: Settings) -> ResponseCache:
    global _cache
    _cache = ResponseCache.from_settings(settings)
    return _cache


def get_cache() -> ResponseCache:
    return _cache


async def commit_and_invalidate(session: AsyncSession, account_ids: Iterable[uuid.UUID]) -> None:
    """Atomically advance durable generations and commit the related mutation."""
    await invalidate_account_versions(session, account_ids)
    await session.commit()


async def invalidate_account_versions(
    session: AsyncSession, account_ids: Iterable[uuid.UUID]
) -> None:
    """Advance generations within the caller's transaction and authorization context.

    Services accepting managed-stamp reports resolve customer ownership themselves
    before calling this, while they still hold the required elevated transaction.
    """
    for account_id in sorted(set(account_ids), key=str):
        updated_id = (
            await session.execute(
                update(Account)
                .where(Account.id == account_id)
                .values(cache_version=Account.cache_version + 1)
                .returning(Account.id)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one_or_none()
        if updated_id is None:
            raise LookupError(f"cannot invalidate missing account {account_id}")
