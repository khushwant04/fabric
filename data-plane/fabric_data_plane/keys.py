"""Cached verification keys.

AR-DP02: a deployed data plane keeps serving valid already-issued tokens while
the control plane, database, operator, or Auth0 are unavailable. So key material
is cached in memory, may be seeded from disk, and a fetch failure never
invalidates keys already held — it only stops them from being refreshed.

A token signed by a key this process has never seen triggers at most one refresh,
which is the only situation in which a request touches the control plane at all.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx
import jwt

from fabric_data_plane.config import Settings


class SigningKeyUnavailableError(RuntimeError):
    """No usable key matches the token's key id."""


class KeyCache:
    """Holds Fabric's public keys and refreshes them lazily."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client
        self._lock = threading.Lock()
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._fetch_failures = 0
        self._last_error: str | None = None
        self._refreshes = 0

        seeded = settings.load_jwks_file()
        if seeded:
            self._install(json.loads(seeded))

    # -- state -------------------------------------------------------------

    @property
    def key_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._keys)

    def snapshot(self) -> dict[str, Any]:
        """Operational view for the admin listener."""
        with self._lock:
            age = None if self._fetched_at is None else time.monotonic() - self._fetched_at
            return {
                "key_ids": sorted(self._keys),
                "keys_held": len(self._keys),
                "age_seconds": age,
                "refreshes": self._refreshes,
                "fetch_failures": self._fetch_failures,
                "last_error": self._last_error,
                # Serving with stale keys is expected during an outage, so it is
                # reported rather than treated as a failure.
                "serving_from_cache": age is not None
                and age > self._settings.jwks_refresh_seconds,
            }

    # -- internals ---------------------------------------------------------

    def _install(self, document: dict[str, Any]) -> None:
        parsed: dict[str, Any] = {}
        for entry in document.get("keys", []):
            key_id = entry.get("kid")
            if not key_id:
                continue
            try:
                parsed[key_id] = jwt.PyJWK(entry).key
            except Exception:  # noqa: BLE001 - one bad entry must not void the set
                continue
        if parsed:
            with self._lock:
                self._keys = parsed
                self._fetched_at = time.monotonic()

    def _fetch(self) -> bool:
        """Try to refresh. Returns whether new keys were installed."""
        client = self._client
        owned = client is None
        if owned:
            client = httpx.Client(timeout=self._settings.jwks_timeout_seconds)
        try:
            response = client.get(self._settings.jwks_url)
            response.raise_for_status()
            document = response.json()
        except Exception as exc:  # noqa: BLE001 - an outage must not drop cached keys
            with self._lock:
                self._fetch_failures += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
            return False
        finally:
            if owned:
                client.close()

        self._install(document)
        with self._lock:
            self._refreshes += 1
            self._last_error = None
        return True

    def _stale(self) -> bool:
        with self._lock:
            if self._fetched_at is None:
                return True
            return (time.monotonic() - self._fetched_at) >= self._settings.jwks_refresh_seconds

    # -- lookup ------------------------------------------------------------

    def refresh(self) -> bool:
        """Fetch the document now, regardless of staleness.

        Used to warm the cache at startup. Readiness requires key material, and
        keys were previously fetched only while verifying a token, so a pod could
        never become ready: it needed traffic to fetch keys, and it received no
        traffic until it was ready.
        """
        return self._fetch()

    def key_for(self, key_id: str | None) -> Any:
        """Return the verification key for a token's ``kid``.

        Refreshes only when the key is unknown or the cache has expired, so the
        common path performs no network call.
        """
        if key_id:
            with self._lock:
                key = self._keys.get(key_id)
            if key is not None and not self._stale():
                return key

        if self._stale() or (key_id and key_id not in self.key_ids):
            self._fetch()

        with self._lock:
            if key_id:
                key = self._keys.get(key_id)
                if key is not None:
                    return key
            # Rotation overlap means several keys may be valid; without a kid
            # there is nothing to select on.
            if not key_id and len(self._keys) == 1:
                return next(iter(self._keys.values()))

        raise SigningKeyUnavailableError(
            f"no cached verification key for kid={key_id!r}"
        )
