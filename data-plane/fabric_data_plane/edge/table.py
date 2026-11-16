"""The routing table the edge forwards by, and its last-good copy on disk.

The table comes from the control plane's ``GET /v1/edge/routing-table`` (ADR 0021). It
says, for each account, which deployments are placed on which live stamps and at what
public gateway URL. Lookups never cross accounts: every index is keyed by the account id
from the caller's verified token, so another account's deployment cannot be a candidate
whatever the request names.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
import tempfile
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Model names that ask the stamp to choose among the account's deployments.
SELECTOR_MODELS = frozenset({"auto", "cascade"})


@dataclasses.dataclass(frozen=True)
class StampRef:
    """A stamp gateway as the edge reaches it."""

    stamp_id: str
    name: str
    region: str | None
    #: Origin only (scheme, host, port); request paths are appended unchanged.
    origin: str


@dataclasses.dataclass(frozen=True)
class Placement:
    stamp: StampRef
    ready: bool


@dataclasses.dataclass(frozen=True)
class RoutedDeployment:
    account_id: uuid.UUID
    deployment_id: uuid.UUID
    model_alias: str
    placements: tuple[Placement, ...]


def gateway_origin(inference_url: str) -> str | None:
    """The stamp gateway's origin from its reported base URL, or None if unusable.

    Stamps report ``https://host[:port]`` optionally followed by ``/v1``; the edge forwards
    the caller's own path (``/v1/...``), so only the origin is kept. Anything that is not
    HTTPS is refused here as well as at the control plane: the edge forwards bearer tokens.
    """
    parts = urlsplit(inference_url.strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    if parts.path.rstrip("/") not in ("", "/v1"):
        return None
    return f"https://{parts.netloc}"


class RoutingTable:
    """An immutable, indexed view of one routing-table document."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.revision = int(document.get("revision") or 0)
        self.etag = str(document.get("etag") or "")
        self.generated_at = str(document.get("generated_at") or "")
        self._by_account: dict[uuid.UUID, list[RoutedDeployment]] = {}
        self.stamps: dict[str, StampRef] = {}
        skipped = 0
        for raw in document.get("deployments") or []:
            try:
                deployment = self._parse_deployment(raw)
            except (KeyError, TypeError, ValueError):
                skipped += 1
                continue
            if deployment.placements:
                self._by_account.setdefault(deployment.account_id, []).append(deployment)
        if skipped:
            logger.warning("routing table: skipped %d malformed deployment entries", skipped)

    def _parse_deployment(self, raw: dict[str, Any]) -> RoutedDeployment:
        placements: list[Placement] = []
        for entry in raw.get("placements") or []:
            origin = gateway_origin(str(entry["inference_url"]))
            if origin is None:
                continue
            stamp_id = str(uuid.UUID(str(entry["stamp_id"])))
            stamp = StampRef(
                stamp_id=stamp_id,
                name=str(entry.get("stamp_name") or stamp_id),
                region=entry.get("region") or None,
                origin=origin,
            )
            self.stamps[stamp_id] = stamp
            placements.append(Placement(stamp=stamp, ready=bool(entry.get("ready"))))
        return RoutedDeployment(
            account_id=uuid.UUID(str(raw["account_id"])),
            deployment_id=uuid.UUID(str(raw["deployment_id"])),
            model_alias=str(raw["model_alias"]),
            placements=tuple(placements),
        )

    # -- lookups, always within one account ---------------------------------

    def deployments_for(self, account_id: uuid.UUID) -> list[RoutedDeployment]:
        return list(self._by_account.get(account_id, ()))

    def account_stamps(self, account_id: uuid.UUID) -> list[Placement]:
        """Every stamp holding any of the account's deployments, one entry per stamp.

        A stamp counts as ready when any of the account's deployments is ready there.
        """
        merged: dict[str, Placement] = {}
        for deployment in self.deployments_for(account_id):
            for placement in deployment.placements:
                held = merged.get(placement.stamp.stamp_id)
                if held is None or (placement.ready and not held.ready):
                    merged[placement.stamp.stamp_id] = placement
        return sorted(merged.values(), key=lambda item: item.stamp.stamp_id)

    def candidates(self, account_id: uuid.UUID, model: str | None) -> list[Placement]:
        """Stamps that may serve ``model`` for this account; empty when none may.

        A model is matched by alias or by deployment id, only among the account's own
        deployments. ``auto``/``cascade`` (or no model at all) leave the choice of
        deployment to the stamp, so every stamp holding one of the account's deployments
        is a candidate. A real deployment aliased ``auto`` still wins, as on the stamp.
        """
        owned = self.deployments_for(account_id)
        if model:
            exact = [item for item in owned if item.model_alias == model]
            if not exact:
                try:
                    wanted = uuid.UUID(model)
                except ValueError:
                    wanted = None
                exact = [item for item in owned if wanted and item.deployment_id == wanted]
            if exact:
                merged: dict[str, Placement] = {}
                for deployment in exact:
                    for placement in deployment.placements:
                        held = merged.get(placement.stamp.stamp_id)
                        if held is None or (placement.ready and not held.ready):
                            merged[placement.stamp.stamp_id] = placement
                return sorted(merged.values(), key=lambda item: item.stamp.stamp_id)
            if model not in SELECTOR_MODELS:
                return []
        return self.account_stamps(account_id)

    def model_names(self, account_id: uuid.UUID) -> list[str]:
        return sorted({item.model_alias for item in self.deployments_for(account_id)})

    # -- reporting ------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Counts per stamp. No account ids, aliases or anything secret."""
        per_stamp: dict[str, dict[str, Any]] = {}
        deployments = 0
        for owned in self._by_account.values():
            for deployment in owned:
                deployments += 1
                for placement in deployment.placements:
                    entry = per_stamp.setdefault(
                        placement.stamp.stamp_id,
                        {
                            "stamp_id": placement.stamp.stamp_id,
                            "name": placement.stamp.name,
                            "region": placement.stamp.region,
                            "origin": placement.stamp.origin,
                            "placements": 0,
                            "ready_placements": 0,
                        },
                    )
                    entry["placements"] += 1
                    entry["ready_placements"] += int(placement.ready)
        return {
            "revision": self.revision,
            "etag": self.etag,
            "generated_at": self.generated_at,
            "accounts": len(self._by_account),
            "deployments": deployments,
            "stamps": sorted(per_stamp.values(), key=lambda item: item["name"]),
        }


class TableStore:
    """The table in force, where it came from, and its copy on disk."""

    def __init__(self, cache_path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._table: RoutingTable | None = None
        self._source: str | None = None
        self._loaded_monotonic: float | None = None
        self._loaded_wall: float | None = None
        self.cache_path = pathlib.Path(cache_path) if cache_path else None

    @property
    def table(self) -> RoutingTable | None:
        with self._lock:
            return self._table

    @property
    def source(self) -> str | None:
        return self._source

    def age_seconds(self) -> float | None:
        """Seconds since the table in force was last confirmed by the control plane."""
        with self._lock:
            if self._loaded_wall is None:
                return None
            return max(0.0, time.time() - self._loaded_wall)

    def install(self, document: dict[str, Any], *, source: str, persist: bool = True) -> None:
        table = RoutingTable(document)
        with self._lock:
            self._table = table
            self._source = source
            self._loaded_monotonic = time.monotonic()
            self._loaded_wall = time.time()
        if persist:
            self._persist(document)

    def confirm(self) -> None:
        """The control plane answered 304: the table in force is current."""
        with self._lock:
            if self._table is not None:
                self._loaded_monotonic = time.monotonic()
                self._loaded_wall = time.time()
                self._source = "control-plane"

    def load_from_disk(self) -> bool:
        if self.cache_path is None or not self.cache_path.exists():
            return False
        try:
            saved = json.loads(self.cache_path.read_text())
            document = saved["table"]
            table = RoutingTable(document)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("could not load the cached routing table: %s", exc)
            return False
        with self._lock:
            self._table = table
            self._source = "disk"
            self._loaded_monotonic = time.monotonic()
            # The age reported for a table read from disk is the age it had when saved.
            self._loaded_wall = float(saved.get("saved_at") or time.time())
        logger.info(
            "serving the cached routing table from %s (revision %s)",
            self.cache_path,
            table.revision,
        )
        return True

    def _persist(self, document: dict[str, Any]) -> None:
        if self.cache_path is None:
            return
        payload = json.dumps({"saved_at": time.time(), "table": document}, separators=(",", ":"))
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            # Written beside the target and renamed over it, so a crash mid-write never
            # leaves a truncated table for the next start to choke on.
            handle, temporary = tempfile.mkstemp(
                dir=self.cache_path.parent, prefix=".routing-table."
            )
            try:
                with os.fdopen(handle, "w") as stream:
                    stream.write(payload)
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.cache_path)
            except BaseException:
                pathlib.Path(temporary).unlink(missing_ok=True)
                raise
        except OSError as exc:
            # Serving continues from memory; only restart-during-outage is affected.
            logger.warning("could not persist the routing table to %s: %s", self.cache_path, exc)
