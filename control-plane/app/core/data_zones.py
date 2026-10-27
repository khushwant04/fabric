"""Data zones: where a deployment's prompts and completions are allowed to be processed.

A deployment declares one of three residency levels (ADR 0017):

``global``
    Any stamp may serve it. This is what every deployment created before residency existed
    gets, so adding the field changes nothing for them.
``data_zone``
    Only stamps whose region lies inside one named zone, such as ``eu`` or ``us``. The zone
    is a legal and contractual boundary (GDPR, a national data law), not a latency one.
``regional``
    Only stamps in an explicit list of regions. This is also the escape hatch for regions
    the catalogue does not know, such as an on-premises cluster named after its building.

Three ideas carry the module:

*The catalogue decides, not the stamp.* A stamp reports its region; which zone that region
belongs to is answered here. A stamp cannot declare itself "in the EU" — it can only name a
region, and the mapping from region to zone is held centrally where it can be reviewed.

*Zones are disjoint.* Every region belongs to at most one zone. Overlapping zones (``jp``
inside ``apac``) would make "is this stamp in the zone" depend on which zone was asked about
first, so a country with its own data law has its own zone and ``apac`` means the rest.

*Unknown fails closed.* A region the catalogue does not recognise belongs to no zone, so it
satisfies ``global`` and an explicit ``regional`` list naming it, and nothing else. Treating
an unknown region as "probably fine" is exactly the error a residency control exists to
prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

ResidencyLevel = Literal["global", "data_zone", "regional"]
RESIDENCY_LEVELS: tuple[str, ...] = ("global", "data_zone", "regional")

#: What a region name may look like once normalised. Covers the Azure (``westeurope``),
#: AWS (``eu-west-1``) and GCP (``europe-west4``) spellings as well as custom names.
REGION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


@dataclass(frozen=True)
class DataZone:
    """One named zone and the provider regions inside it."""

    key: str
    name: str
    regions: frozenset[str] = field(default_factory=frozenset)


def _zone(key: str, name: str, *regions: str) -> DataZone:
    return DataZone(key=key, name=name, regions=frozenset(regions))


#: The catalogue. Region names are written exactly as each provider spells them; the three
#: providers' naming schemes do not collide, so one flat set per zone is unambiguous.
DATA_ZONES: dict[str, DataZone] = {
    zone.key: zone
    for zone in (
        _zone(
            "us",
            "United States",
            # Azure
            "eastus", "eastus2", "centralus", "northcentralus", "southcentralus",
            "westcentralus", "westus", "westus2", "westus3",
            # AWS
            "us-east-1", "us-east-2", "us-west-1", "us-west-2",
            # GCP
            "us-central1", "us-east1", "us-east4", "us-east5", "us-south1",
            "us-west1", "us-west2", "us-west3", "us-west4",
        ),
        _zone(
            "eu",
            "European Union and EEA",
            # Azure
            "northeurope", "westeurope", "francecentral", "francesouth",
            "germanywestcentral", "germanynorth", "italynorth", "polandcentral",
            "spaincentral", "swedencentral", "swedensouth", "norwayeast", "norwaywest",
            # AWS
            "eu-central-1", "eu-west-1", "eu-west-3", "eu-north-1", "eu-south-1",
            "eu-south-2",
            # GCP
            "europe-west1", "europe-west3", "europe-west4", "europe-west8",
            "europe-west9", "europe-west10", "europe-west12", "europe-north1",
            "europe-central2", "europe-southwest1",
        ),
        _zone("uk", "United Kingdom", "uksouth", "ukwest", "eu-west-2", "europe-west2"),
        _zone(
            "ch",
            "Switzerland",
            "switzerlandnorth", "switzerlandwest", "eu-central-2", "europe-west6",
        ),
        _zone(
            "ca",
            "Canada",
            "canadacentral", "canadaeast", "ca-central-1", "ca-west-1",
            "northamerica-northeast1", "northamerica-northeast2",
        ),
        _zone(
            "latam",
            "Latin America",
            "brazilsouth", "brazilsoutheast", "mexicocentral", "chilecentral",
            "sa-east-1", "mx-central-1", "southamerica-east1", "southamerica-west1",
        ),
        _zone(
            "jp",
            "Japan",
            "japaneast", "japanwest", "ap-northeast-1", "ap-northeast-3",
            "asia-northeast1", "asia-northeast2",
        ),
        _zone(
            "kr",
            "South Korea",
            "koreacentral", "koreasouth", "ap-northeast-2", "asia-northeast3",
        ),
        _zone(
            "in",
            "India",
            "centralindia", "southindia", "westindia", "jioindiawest", "jioindiacentral",
            "ap-south-1", "ap-south-2", "asia-south1", "asia-south2",
        ),
        _zone(
            "au",
            "Australia",
            "australiaeast", "australiasoutheast", "australiacentral", "australiacentral2",
            "ap-southeast-2", "ap-southeast-4", "australia-southeast1",
            "australia-southeast2",
        ),
        _zone(
            "apac",
            "Asia Pacific (other)",
            "southeastasia", "eastasia", "indonesiacentral", "malaysiawest",
            "ap-southeast-1", "ap-southeast-3", "ap-southeast-5", "ap-east-1",
            "asia-southeast1", "asia-southeast2", "asia-east1", "asia-east2",
        ),
        _zone(
            "me",
            "Middle East",
            "uaenorth", "uaecentral", "qatarcentral", "israelcentral",
            "me-central-1", "me-south-1", "il-central-1", "me-central1", "me-west1",
        ),
        _zone(
            "af",
            "Africa",
            "southafricanorth", "southafricawest", "af-south-1", "africa-south1",
        ),
    )
}

_REGION_TO_ZONE: dict[str, str] = {}
for _key, _data_zone in DATA_ZONES.items():
    for _region in _data_zone.regions:
        if _region in _REGION_TO_ZONE:  # pragma: no cover - guarded by a test as well
            raise RuntimeError(f"region {_region!r} is in two data zones")
        _REGION_TO_ZONE[_region] = _key


def normalise_region(value: str | None) -> str | None:
    """Fold a region to the catalogue's spelling, or ``None`` when there is none."""
    if value is None:
        return None
    folded = value.strip().lower().replace("_", "-")
    return folded or None


def normalise_zone(value: str | None) -> str | None:
    if value is None:
        return None
    folded = value.strip().lower()
    return folded or None


def zone_of(region: str | None) -> str | None:
    """The zone a region belongs to, or ``None`` when the catalogue does not know it."""
    normalised = normalise_region(region)
    if normalised is None:
        return None
    return _REGION_TO_ZONE.get(normalised)


def is_known_zone(value: str | None) -> bool:
    return normalise_zone(value) in DATA_ZONES


@dataclass(frozen=True)
class Residency:
    """A deployment's residency requirement, already validated and normalised."""

    level: ResidencyLevel = "global"
    data_zone: str | None = None
    regions: tuple[str, ...] = ()

    @property
    def restricted(self) -> bool:
        return self.level != "global"

    def refusal(self, region: str | None) -> str | None:
        """Why a stamp in ``region`` may not serve this deployment, or ``None`` if it may."""
        if self.level == "global":
            return None
        normalised = normalise_region(region)
        if normalised is None:
            # A stamp that has not said where it is cannot be shown to be inside anything.
            return "stamp_region_unknown"
        if self.level == "regional":
            return None if normalised in self.regions else "stamp_region_not_permitted"
        stamp_zone = _REGION_TO_ZONE.get(normalised)
        if stamp_zone is None:
            return "stamp_region_not_in_catalogue"
        return None if stamp_zone == self.data_zone else "stamp_outside_data_zone"

    def as_details(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "data_zone": self.data_zone,
            "regions": list(self.regions),
        }


GLOBAL = Residency()


def residency_of(spec: dict[str, Any] | None) -> Residency:
    """Read residency from a stored deployment spec.

    Stored specs were validated by the API when written, so this reads rather than
    validates. A spec written before residency existed has no key and is ``global``, which is
    what it always was. A malformed value — which only a direct database edit can produce —
    is read as the strictest possible policy rather than as ``global``: an empty regional
    list that no stamp satisfies. Misreading a damaged restriction as "anywhere" would be the
    one failure this feature must not have.
    """
    raw = (spec or {}).get("residency")
    if raw is None:
        return GLOBAL
    if not isinstance(raw, dict):
        return Residency(level="regional", regions=())
    level = raw.get("level", "global")
    if level == "global":
        return GLOBAL
    if level == "data_zone":
        named = raw.get("data_zone")
        zone = normalise_zone(named) if isinstance(named, str) else None
        if zone in DATA_ZONES:
            return Residency(level="data_zone", data_zone=zone)
        return Residency(level="regional", regions=())
    if level == "regional":
        regions = raw.get("regions")
        if isinstance(regions, list):
            folded = tuple(
                sorted(
                    {
                        region
                        for region in (
                            normalise_region(item) for item in regions if isinstance(item, str)
                        )
                        if region is not None
                    }
                )
            )
            return Residency(level="regional", regions=folded)
    return Residency(level="regional", regions=())


def boundary_of(region: str | None) -> dict[str, str | None]:
    """The stamp-wide boundary a data plane enforces: its region and the zone it is in."""
    normalised = normalise_region(region)
    return {"region": normalised, "data_zone": zone_of(normalised)}


def catalogue() -> list[dict[str, Any]]:
    """The zones and their regions, for the public catalogue endpoint."""
    return [
        {"key": zone.key, "name": zone.name, "regions": sorted(zone.regions)}
        for zone in sorted(DATA_ZONES.values(), key=lambda zone: zone.key)
    ]
