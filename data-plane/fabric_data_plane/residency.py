"""Data-zone enforcement at the gateway (ADR 0017).

Placement already keeps a zone-restricted deployment off stamps outside its zone. The
gateway checks again on every request, because placement is a decision made once while
requests keep arriving: a stamp can report a different region after it was admitted, and
a client holding a zone-bound token can point it at the wrong stamp. Both must be refused
here, before the prompt reaches a model host.

The gateway holds no zone catalogue. The control plane reads the stamp's region, looks up
its zone, and the agent writes both into ``deployments.json`` as ``data_boundary``. Holding
a second copy of the catalogue here would let the two disagree, and a disagreement about
which country a request may be processed in is not one to resolve by choosing whichever
copy is newer.

Everything fails closed. No boundary, an unknown zone, or a residency level this process
does not recognise all refuse restricted work; only ``global`` deployments and unbound
tokens are served without a boundary, which is exactly what they were before.
"""

from __future__ import annotations

import dataclasses
from typing import Any

LEVELS = frozenset({"global", "data_zone", "regional"})
_MAX_ZONE_LENGTH = 16


@dataclasses.dataclass(frozen=True)
class DataBoundary:
    """Where this stamp is, as the control plane's catalogue reads it."""

    region: str | None = None
    data_zone: str | None = None

    def as_details(self) -> dict[str, str | None]:
        return {"region": self.region, "data_zone": self.data_zone}


@dataclasses.dataclass(frozen=True)
class Residency:
    """Where one deployment's requests may be processed."""

    level: str = "global"
    data_zone: str | None = None
    regions: tuple[str, ...] = ()

    @property
    def restricted(self) -> bool:
        return self.level != "global"


GLOBAL = Residency()
#: What an unreadable residency means: a regional list nothing can satisfy.
_UNSATISFIABLE = Residency(level="regional", regions=())


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    folded = value.strip().lower()
    return folded or None


def residency_from_entry(raw: Any) -> Residency:
    """Parse an entry's ``residency``; absent is global and anything unreadable is strictest."""
    if raw is None:
        return GLOBAL
    if not isinstance(raw, dict):
        return _UNSATISFIABLE
    level = _text(raw.get("level")) or "global"
    if level not in LEVELS:
        # A newer control plane may have a level this gateway cannot evaluate. Serving it as
        # global would turn a restriction this process does not understand into none at all.
        return _UNSATISFIABLE
    if level == "global":
        return GLOBAL
    if level == "data_zone":
        zone = _text(raw.get("data_zone"))
        return Residency(level="data_zone", data_zone=zone) if zone else _UNSATISFIABLE
    regions = raw.get("regions")
    if not isinstance(regions, list):
        return _UNSATISFIABLE
    folded = tuple(sorted({region for region in map(_text, regions) if region}))
    return Residency(level="regional", regions=folded)


def boundary_from_payload(payload: dict[str, Any]) -> DataBoundary | None:
    """Read the document's stamp-wide boundary, or ``None`` when it carries none."""
    raw = payload.get("data_boundary")
    if not isinstance(raw, dict):
        return None
    return DataBoundary(region=_text(raw.get("region")), data_zone=_text(raw.get("data_zone")))


def residency_refusal(residency: Residency, boundary: DataBoundary | None) -> str | None:
    """Why this stamp may not serve this deployment, or ``None`` if it may."""
    if not residency.restricted:
        return None
    if boundary is None:
        return "data_boundary_unknown"
    if residency.level == "regional":
        if boundary.region is not None and boundary.region in residency.regions:
            return None
        return "stamp_region_not_permitted"
    if boundary.data_zone is not None and boundary.data_zone == residency.data_zone:
        return None
    return "stamp_outside_data_zone"


def zone_refusal(required: str | None, boundary: DataBoundary | None) -> str | None:
    """Why a token or request bound to ``required`` may not be served here."""
    if required is None:
        return None
    if boundary is None:
        return "data_boundary_unknown"
    if boundary.data_zone != required:
        return "stamp_outside_data_zone"
    return None


def requested_zone(value: Any) -> str:
    """Validate a client's ``routing.data_zone`` value, raising ``ValueError`` if malformed."""
    zone = _text(value)
    if zone is None or len(zone) > _MAX_ZONE_LENGTH:
        raise ValueError("routing.data_zone must be a data zone name such as 'eu'")
    return zone
