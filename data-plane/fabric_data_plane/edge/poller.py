"""Background work: polling the routing table and refreshing verification keys.

Both run off the request path. A control-plane outage stops them from refreshing and
nothing else: the edge keeps serving the table and keys it already holds (ADR 0001).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from fabric_data_plane.config import Settings as GatewaySettings
from fabric_data_plane.edge.config import EdgeSettings
from fabric_data_plane.edge.metrics import EdgeMetrics
from fabric_data_plane.edge.table import TableStore
from fabric_data_plane.keys import KeyCache

logger = logging.getLogger(__name__)

ROUTING_TABLE_PATH = "/v1/edge/routing-table"
CONTROL_AUDIENCE = "fabric-control"


class EdgeKeyCache(KeyCache):
    """The gateway's key cache, with fetches triggered by requests rate-limited.

    A token naming an unknown key id makes the shared cache fetch the key set, which is
    right for rotation and wrong for an edge facing the internet: made-up key ids would
    become requests to the control plane. Explicit refreshes (startup, the background
    task) are never limited.
    """

    def __init__(
        self, settings: GatewaySettings, *, min_refetch_seconds: float, **kwargs: Any
    ) -> None:
        self._min_refetch = min_refetch_seconds
        self._last_attempt = float("-inf")
        super().__init__(settings, **kwargs)

    def _fetch(self, source: str | None = None, *, forced: bool = False) -> bool:  # type: ignore[override]
        now = time.monotonic()
        if not forced and now - self._last_attempt < self._min_refetch:
            return False
        self._last_attempt = now
        return super()._fetch(source)

    def refresh(self) -> bool:
        return self._fetch(forced=True)


class ControlPlaneClient:
    """Exchanges the edge credential for a control token and reads the routing table."""

    def __init__(self, settings: EdgeSettings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self._token: str | None = None
        self._token_expires = 0.0

    async def _access_token(self, *, force: bool = False) -> str:
        if not force and self._token and time.monotonic() < self._token_expires:
            return self._token
        api_key = self.settings.resolved_api_key()
        if not api_key:
            raise RuntimeError("no edge credential configured (FABRIC_EDGE_API_KEY_FILE)")
        response = await self.client.post(
            f"{self.settings.control_plane_url}/v1/token",
            json={"grant_type": "api_key", "api_key": api_key, "audience": CONTROL_AUDIENCE},
            timeout=self.settings.poll_timeout_seconds,
        )
        if response.status_code != 200:
            # The body may echo request details; only the status is logged.
            raise RuntimeError(f"token exchange refused with HTTP {response.status_code}")
        body = response.json()
        self._token = str(body["access_token"])
        # Renewed a minute early so a poll never presents a token that expires in flight.
        self._token_expires = time.monotonic() + max(30.0, float(body.get("expires_in", 300)) - 60)
        return self._token

    async def fetch(self, etag: str | None) -> tuple[int, dict[str, Any] | None]:
        """Return ``(status, document)``; the document is None for 304."""
        for attempt in range(2):
            token = await self._access_token(force=attempt > 0)
            headers = {"Authorization": f"Bearer {token}"}
            if etag:
                headers["If-None-Match"] = etag
            response = await self.client.get(
                f"{self.settings.control_plane_url}{ROUTING_TABLE_PATH}",
                headers=headers,
                timeout=self.settings.poll_timeout_seconds,
            )
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code == 304:
                return 304, None
            if response.status_code != 200:
                raise RuntimeError(f"routing table refused with HTTP {response.status_code}")
            return 200, response.json()
        raise RuntimeError("routing table refused the renewed token")


class TablePoller:
    """Keeps the table in force current, refusing anything older than it."""

    def __init__(
        self,
        settings: EdgeSettings,
        store: TableStore,
        client: ControlPlaneClient,
        metrics: EdgeMetrics,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self.metrics = metrics
        self.last_error: str | None = None
        self.last_success: float | None = None
        self._regressed_since: float | None = None

    async def poll_once(self) -> str:
        current = self.store.table
        try:
            status, document = await self.client.fetch(current.etag if current else None)
        except Exception as exc:  # noqa: BLE001 - an outage must never drop the table
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.metrics.table_fetch("error")
            logger.warning("routing table poll failed, serving the table held: %s", self.last_error)
            return "error"

        self.last_error = None
        self.last_success = time.time()
        if status == 304:
            self.store.confirm()
            self.metrics.table_fetch("not_modified")
            return "not_modified"

        assert document is not None
        revision = int(document.get("revision") or 0)
        if current is not None and revision < current.revision:
            # A lagging replica or read replica must not roll the table back. A control
            # plane that stays behind for longer than the grace period has most likely been
            # restored, and its view is accepted rather than refused forever.
            now = time.monotonic()
            self._regressed_since = self._regressed_since or now
            if now - self._regressed_since < self.settings.revision_regression_grace_seconds:
                self.metrics.table_fetch("stale_revision")
                logger.warning(
                    "refused routing table revision %s older than held %s",
                    revision,
                    current.revision,
                )
                return "stale_revision"
            logger.warning("accepting regressed routing table revision %s", revision)
        self._regressed_since = None
        self.store.install(document, source="control-plane")
        self.metrics.table_fetch("updated")
        return "updated"

    async def run(self) -> None:
        while True:
            await self.poll_once()
            interval = self.settings.poll_interval_seconds
            await asyncio.sleep(interval + random.uniform(0, interval * self.settings.poll_jitter))


async def refresh_keys(keys: KeyCache, settings: EdgeSettings) -> None:
    """Fetch keys until some are held, then refresh on the configured interval."""
    delay = 1.0
    while True:
        if keys.snapshot()["keys_held"] == 0:
            await asyncio.to_thread(keys.refresh)
            if keys.snapshot()["keys_held"] == 0:
                await asyncio.sleep(delay)
                delay = min(delay * 2, float(settings.jwks_refresh_seconds))
                continue
            logger.info("verification keys loaded")
        await asyncio.sleep(settings.jwks_refresh_seconds)
        await asyncio.to_thread(keys.refresh)
