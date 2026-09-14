"""Locally configured deployments.

AR-ID04: the data plane resolves deployments from local resource configuration.
The cluster agent writes this file from desired state it pulled while it had
connectivity; serving a request never consults the control plane.

A client selects a deployment by model name, which is ordinary OpenAI-compatible
behaviour and carries no authority. Ownership is decided by comparing the
verified token's account against the account recorded here, so naming another
account's deployment is rejected rather than served.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
import threading
import time
import uuid
from typing import Any

from fabric_data_plane.errors import Forbidden, NotFound
from fabric_data_plane.pool import (
    DEFAULT_STRATEGY,
    Backend,
    BackendHealth,
    BackendPool,
)

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Deployment:
    """One locally served deployment.

    A deployment is served by a *pool* of backends (ADR 0010). For backward
    compatibility a single ``upstream_url`` is still accepted and becomes a one-backend
    pool: constructing ``Deployment(..., upstream_url=...)`` keeps working, and so does
    the existing single-``upstream_url`` ``deployments.json`` shape. Supplying
    ``backends`` (a list of :class:`~fabric_data_plane.pool.Backend`) is the fleet shape;
    the two are mutually exclusive inputs and ``backends`` wins when both are present.
    """

    deployment_id: uuid.UUID
    account_id: uuid.UUID
    model_alias: str
    upstream_url: str | None = None
    upstream_model: str | None = None
    backends: tuple[Backend, ...] = ()
    #: How the pool balances across ``backends``. Defaults to least-in-flight, which is
    #: also what an entry that predates the field gets (M2, ADR 0010). The vocabulary is
    #: validated by the control plane; an unknown value here defaults rather than raising.
    strategy: str = DEFAULT_STRATEGY

    def __post_init__(self) -> None:
        if not self.backends:
            if not self.upstream_url:
                raise ValueError("a deployment needs an upstream_url or a backends list")
            url = self.upstream_url.rstrip("/")
            object.__setattr__(self, "backends", (Backend(url=url, backend_id=url),))

    @property
    def upstream_model_name(self) -> str:
        """Model name the upstream host expects, defaulting to the alias."""
        return self.upstream_model or self.model_alias

    def build_pool(
        self, *, failure_threshold: int = 3, recovery_seconds: float = 30.0
    ) -> BackendPool:
        """Build a fresh pool with per-backend health and this deployment's strategy.

        The caller memoises the result so health survives across requests; the pool is
        built here so the backend set and its balancing strategy are defined in one place.
        """
        health = BackendHealth(
            list(self.backends),
            failure_threshold=failure_threshold,
            recovery_seconds=recovery_seconds,
        )
        return BackendPool(list(self.backends), health, strategy=self.strategy)


class DeploymentRegistry:
    """Immutable view of the deployments assigned to this stamp."""

    def __init__(self, deployments: list[Deployment]) -> None:
        self._by_id: dict[uuid.UUID, Deployment] = {}
        self._by_alias: dict[str, Deployment] = {}
        for deployment in deployments:
            self._by_id[deployment.deployment_id] = deployment
            # Aliases are unique per stamp; a later entry replacing an earlier one
            # would make routing ambiguous, so it is rejected.
            existing = self._by_alias.get(deployment.model_alias)
            if existing is not None and existing.deployment_id != deployment.deployment_id:
                raise ValueError(
                    f"model alias {deployment.model_alias!r} is claimed by two deployments"
                )
            self._by_alias[deployment.model_alias] = deployment

    def __len__(self) -> int:
        return len(self._by_id)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> DeploymentRegistry:
        deployments = [
            cls._deployment_from_entry(entry) for entry in payload.get("deployments", [])
        ]
        return cls(deployments)

    @staticmethod
    def _deployment_from_entry(entry: dict[str, Any]) -> Deployment:
        """Parse one configuration entry into a Deployment.

        Accepts both shapes: a ``backends`` list (each with a ``url`` and an optional
        ``id`` and ``weight``) is the fleet shape, and a single ``upstream_url`` is the
        legacy shape that becomes a one-backend pool. ``backends`` wins if both appear,
        so a configuration mid-migration is unambiguous.
        """
        raw_backends = entry.get("backends")
        backends: tuple[Backend, ...] = ()
        if raw_backends:
            parsed: list[Backend] = []
            for item in raw_backends:
                url = str(item["url"]).rstrip("/")
                parsed.append(
                    Backend(
                        url=url,
                        backend_id=str(item.get("id") or url),
                        weight=float(item.get("weight", 1.0)),
                    )
                )
            backends = tuple(parsed)

        upstream_url = entry.get("upstream_url")
        strategy = entry.get("strategy")
        return Deployment(
            deployment_id=uuid.UUID(str(entry["deployment_id"])),
            account_id=uuid.UUID(str(entry["account_id"])),
            model_alias=str(entry["model_alias"]),
            upstream_url=str(upstream_url).rstrip("/") if upstream_url else None,
            upstream_model=entry.get("upstream_model"),
            backends=backends,
            strategy=str(strategy) if strategy else DEFAULT_STRATEGY,
        )

    @classmethod
    def load(cls, path: str | pathlib.Path) -> DeploymentRegistry:
        location = pathlib.Path(path)
        if not location.exists():
            # An empty registry is a valid state: the agent has not yet delivered
            # any placement to this stamp.
            return cls([])
        return cls.from_payload(json.loads(location.read_text()))

    def resolve(self, model: str, *, account_id: uuid.UUID) -> Deployment:
        """Return the deployment this account may invoke under ``model``.

        Raises ``NotFound`` when nothing is served under that name here, and
        ``Forbidden`` when it belongs to another account.
        """
        deployment = self._by_alias.get(model)
        if deployment is None:
            try:
                deployment = self._by_id.get(uuid.UUID(model))
            except ValueError:
                deployment = None
        if deployment is None:
            raise NotFound("model_not_found", "No such model is served by this stamp")
        if deployment.account_id != account_id:
            # Deliberately the same shape of answer as an unknown model would
            # give a caller, minus the leak: they learn nothing about another
            # account's deployments beyond being refused.
            raise Forbidden("model_not_available", "Model is not available to this account")
        return deployment

    def deployment_ids(self) -> frozenset[uuid.UUID]:
        """Stable identities currently present, used to retire per-deployment state."""
        return frozenset(self._by_id)

    def for_account(self, account_id: uuid.UUID) -> list[Deployment]:
        """Deployments this account may list."""
        return sorted(
            (d for d in self._by_id.values() if d.account_id == account_id),
            key=lambda d: d.model_alias,
        )


class ReloadingRegistry:
    """A registry that follows the file the cluster agent rewrites.

    The agent re-renders ``deployments.json`` whenever placement changes, so a
    registry read once at startup would serve a snapshot from boot: a newly placed
    deployment would return ``model_not_found`` until the pod restarted, and a
    withdrawn one would keep being served. Both are wrong and neither is visible
    locally, where the file is written before the process starts.

    The file is re-read only when its modification time or size changes, and at most
    once per ``min_interval``, so the common path costs one ``stat``.

    A read that fails keeps the last good registry rather than emptying it. The agent
    writes atomically, so a partial read should not occur; if the file is
    nevertheless unreadable, continuing to serve what was last valid is better than
    refusing traffic the stamp is placed to handle. This mirrors the key cache, which
    also never discards good material on a failed refresh.
    """

    def __init__(self, path: str | pathlib.Path, *, min_interval: float = 1.0) -> None:
        self._path = pathlib.Path(path)
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._registry = DeploymentRegistry.load(self._path)
        self._signature = self._stat()
        self._checked_at = time.monotonic()
        self._reloads = 0
        self._failures = 0
        self._last_error: str | None = None

    def _stat(self) -> tuple[float, int] | None:
        try:
            info = self._path.stat()
        except OSError:
            return None
        return (info.st_mtime, info.st_size)

    def _current(self) -> DeploymentRegistry:
        now = time.monotonic()
        with self._lock:
            if now - self._checked_at < self._min_interval:
                return self._registry
            self._checked_at = now

            signature = self._stat()
            if signature == self._signature:
                return self._registry

            if signature is None:
                # The file vanished. That is not how the agent says "nothing is
                # placed here": it writes an empty list for that. A missing file
                # means the volume or the writer is in trouble, so the last good
                # registry is kept rather than dropping every deployment.
                self._failures += 1
                self._last_error = f"{self._path} is missing"
                logger.warning(
                    "%s is missing, keeping %d known deployments",
                    self._path,
                    len(self._registry),
                )
                return self._registry

            try:
                loaded = DeploymentRegistry.load(self._path)
            except (OSError, ValueError) as error:
                self._failures += 1
                self._last_error = str(error)
                # The signature is recorded even though the parse failed, so the
                # same broken content is not re-parsed on every request. A later
                # write changes it and is retried.
                self._signature = signature
                logger.warning(
                    "could not reload %s, keeping %d known deployments: %s",
                    self._path,
                    len(self._registry),
                    error,
                )
                return self._registry

            self._registry = loaded
            self._signature = signature
            self._reloads += 1
            logger.info("reloaded %s: %d deployments", self._path, len(loaded))
            return self._registry

    # The data plane treats this like a registry, so the read surface matches.
    def resolve(self, model: str, *, account_id: uuid.UUID) -> Deployment:
        return self._current().resolve(model, account_id=account_id)

    def deployment_ids(self) -> frozenset[uuid.UUID]:
        return self._current().deployment_ids()

    def for_account(self, account_id: uuid.UUID) -> list[Deployment]:
        return self._current().for_account(account_id)

    def __len__(self) -> int:
        return len(self._current())

    def snapshot(self) -> dict[str, Any]:
        self._current()
        with self._lock:
            return {
                "path": str(self._path),
                "deployments": len(self._registry),
                "reloads": self._reloads,
                "reload_failures": self._failures,
                "last_error": self._last_error,
            }
