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
import pathlib
import uuid
from typing import Any

from fabric_data_plane.errors import Forbidden, NotFound


@dataclasses.dataclass(frozen=True)
class Deployment:
    """One locally served deployment."""

    deployment_id: uuid.UUID
    account_id: uuid.UUID
    model_alias: str
    upstream_url: str
    upstream_model: str | None = None

    @property
    def upstream_model_name(self) -> str:
        """Model name the upstream host expects, defaulting to the alias."""
        return self.upstream_model or self.model_alias


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
            Deployment(
                deployment_id=uuid.UUID(str(entry["deployment_id"])),
                account_id=uuid.UUID(str(entry["account_id"])),
                model_alias=str(entry["model_alias"]),
                upstream_url=str(entry["upstream_url"]).rstrip("/"),
                upstream_model=entry.get("upstream_model"),
            )
            for entry in payload.get("deployments", [])
        ]
        return cls(deployments)

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

    def for_account(self, account_id: uuid.UUID) -> list[Deployment]:
        """Deployments this account may list."""
        return sorted(
            (d for d in self._by_id.values() if d.account_id == account_id),
            key=lambda d: d.model_alias,
        )
