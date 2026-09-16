"""Whether a stamp can hold a deployment, and which stamp can (ADR 0013).

Before this module, ``create_placement`` only *authorized* a stamp. A deployment asking for
eight H100s onto a single-T4 stamp was accepted, delivered, and never served: the pod stayed
``Pending`` and the API had already answered ``201 assigned``. Every input needed to answer
the question was being collected and ignored — ``resources.gpu_count``, ``gpu_class``,
``allocatable_gpus``, ``requested_gpus``, ``region``.

Three ideas carry the whole file:

*The weakest device decides.* A pod may be scheduled onto any GPU node the operator's
selector allows, so a stamp is admitted at its worst device rather than its best. A guarantee
that only holds on the best node in a mixed pool is a guarantee that fails intermittently,
which is worse than being refused. It is the same rule the operator already applies when it
picks a dtype.

*A class is a minimum, not a product.* ``gpu_class`` means "at least this much memory, at
least this arithmetic". Matching product strings would make ``a100`` and
``NVIDIA-A100-SXM4-40GB`` different classes, and comparing exact compute capability would
refuse an A100 (8.0, 40 GiB) for an ``a10`` request (8.6, 24 GiB) even though it is better in
both properties that decide whether a model can be served at all.

*Commitment is counted from our own records.* A placement is committed the moment it is
written; the stamp reports the consequence a heartbeat later. Admitting against reported
usage alone would let every placement in a burst see the same free capacity.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import elevated
from app.models import Deployment, DeploymentPlacement, InferenceStamp

_GIB = 1024**3

#: How stale a heartbeat may be before automatic selection stops choosing a stamp.
#:
#: The agent's default poll is 15 seconds, so this is twenty missed beats: long enough that a
#: slow reconcile or a restart does not make a stamp disappear, short enough that work is not
#: sent to a cluster that has gone. It is deliberately not applied to an explicitly named
#: stamp — see ``check_fit``.
LIVENESS_WINDOW = dt.timedelta(minutes=5)

#: Capability floors that change what the hardware can execute, rather than exact versions.
#:
#: 8.0 is where bfloat16 exists at all; below it a server asked for it exits before serving.
#: 8.9 is where fp8 exists. Between those boundaries a higher number buys nothing a serving
#: stack depends on, so comparing exact capabilities would reject better devices.
_CAPABILITY_TIERS: tuple[tuple[int, int], ...] = ((8, 9), (8, 0), (7, 0))

#: Memory is compared with a tolerance because nominal marketing sizes are not what the
#: device advertises: a "16 GB" T4 reports 15360 MiB.
_MEMORY_TOLERANCE = 0.9


@dataclass(frozen=True)
class GpuClass:
    """What a named class requires: a frame buffer and an arithmetic tier."""

    memory_bytes: int
    capability: tuple[int, int]

    @property
    def capability_floor(self) -> tuple[int, int]:
        for tier in _CAPABILITY_TIERS:
            if self.capability >= tier:
                return tier
        return (0, 0)

    @property
    def minimum_memory_bytes(self) -> int:
        return int(self.memory_bytes * _MEMORY_TOLERANCE)


#: The classes a deployment may ask for. A catalogue rather than a free string, because an
#: unrecognised class silently meaning "no constraint" is the defect this module removes.
GPU_CLASSES: dict[str, GpuClass] = {
    "v100": GpuClass(16 * _GIB, (7, 0)),
    "t4": GpuClass(16 * _GIB, (7, 5)),
    "rtx-a4000": GpuClass(16 * _GIB, (8, 6)),
    "a10": GpuClass(24 * _GIB, (8, 6)),
    "a10g": GpuClass(24 * _GIB, (8, 6)),
    "l4": GpuClass(24 * _GIB, (8, 9)),
    "a100": GpuClass(40 * _GIB, (8, 0)),
    "a100-80gb": GpuClass(80 * _GIB, (8, 0)),
    "l40s": GpuClass(48 * _GIB, (8, 9)),
    "h100": GpuClass(80 * _GIB, (9, 0)),
}

#: Reported product strings mapped to a class, used *only* to fill in memory or capability a
#: stamp did not report. An agent that can identify a device reports both numbers directly,
#: so this is a fallback for an older agent rather than the primary path.
_PRODUCT_CLASSES: dict[str, str] = {
    "tesla t4": "t4",
    "nvidia t4": "t4",
    "nvidia tesla t4": "t4",
    "nvidia v100": "v100",
    "tesla v100": "v100",
    "nvidia a10": "a10",
    "nvidia a10g": "a10g",
    "nvidia a100": "a100",
    "nvidia a100 40gb": "a100",
    "nvidia a100 80gb": "a100-80gb",
    "nvidia l4": "l4",
    "nvidia l40s": "l40s",
    "nvidia h100": "h100",
    "nvidia rtx a4000": "rtx-a4000",
}


def normalise_class(value: str | None) -> str:
    """Fold a class name to its catalogue key."""
    if not value:
        return ""
    return "-".join(value.strip().lower().replace("_", " ").replace("-", " ").split())


def _normalise_product(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.strip().lower().replace("-", " ").replace("_", " ").split())


def _parse_capability(value: Any) -> tuple[int, int] | None:
    """Read a ``"8.0"`` capability string, or nothing if it is not one."""
    if not isinstance(value, str) or "." not in value:
        return None
    major, _, minor = value.partition(".")
    try:
        return int(major), int(minor)
    except ValueError:
        return None


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


# --- demand ---------------------------------------------------------------


@dataclass(frozen=True)
class Demand:
    """What a deployment needs from a stamp."""

    replicas: int
    gpus_per_replica: int
    gpu_class: str
    region: str | None

    @property
    def total_gpus(self) -> int:
        return self.replicas * self.gpus_per_replica

    def as_details(self) -> dict[str, Any]:
        return {
            "replicas": self.replicas,
            "gpu_count": self.gpus_per_replica,
            "gpus_total": self.total_gpus,
            "gpu_class": self.gpu_class,
            "region": self.region,
        }


def demand_of(spec: dict[str, Any] | None, *, region: str | None = None) -> Demand:
    """Read demand from a deployment's desired spec.

    Absent values fall back to the schema's own defaults rather than to zero, because a
    deployment created before a field existed asked for the single GPU it has always had.
    """
    spec = spec or {}
    resources = spec.get("resources")
    if not isinstance(resources, dict):
        resources = {}
    return Demand(
        replicas=max(1, _positive_int(spec.get("replicas")) or 1),
        gpus_per_replica=max(1, _positive_int(resources.get("gpu_count")) or 1),
        gpu_class=normalise_class(resources.get("gpu_class")) or "t4",
        region=(region or "").strip() or None,
    )


# --- capacity -------------------------------------------------------------


@dataclass(frozen=True)
class StampCapacity:
    """What a stamp has said about itself, reduced to the facts placement needs."""

    allocatable_gpus: int
    #: GPUs claimed by workloads Fabric did not place. Derived by subtracting the stamp's
    #: own report of Fabric-owned claims from its total, so hosts this platform created are
    #: not counted here *and* in ``committed_gpus``.
    foreign_requested_gpus: int
    #: Largest device count on one node, which bounds what a single replica can ask for.
    #: Zero means the stamp did not say.
    max_gpus_per_node: int
    #: Frame buffer and arithmetic of the weakest device reported. ``None`` means the stamp
    #: reported nothing usable, in which case the class requirement cannot be judged.
    weakest_memory_bytes: int | None
    weakest_capability: tuple[int, int] | None
    region: str | None
    last_heartbeat_at: dt.datetime | None

    def free_gpus(self, committed_gpus: int) -> int:
        return self.allocatable_gpus - self.foreign_requested_gpus - committed_gpus

    @property
    def describes_its_hardware(self) -> bool:
        return self.weakest_memory_bytes is not None and self.weakest_capability is not None


def read_capacity(stamp: InferenceStamp) -> StampCapacity:
    """Reduce a stamp's capability report to the facts placement needs."""
    capabilities = stamp.capabilities if isinstance(stamp.capabilities, dict) else {}

    allocatable = _positive_int(capabilities.get("allocatable_gpus"))
    requested = _positive_int(capabilities.get("requested_gpus"))
    fabric_requested = _positive_int(capabilities.get("fabric_requested_gpus"))

    entries = capabilities.get("gpus")
    if not isinstance(entries, list):
        entries = []

    weakest_memory: int | None = None
    weakest_capability: tuple[int, int] | None = None
    described = 0
    for entry in entries:
        if not isinstance(entry, dict) or _positive_int(entry.get("count")) == 0:
            # No devices of this class present, so it says nothing about what can run here.
            continue
        described += 1
        memory = _positive_int(entry.get("memory_bytes"))
        capability = _parse_capability(entry.get("compute_capability"))
        if memory == 0 or capability is None:
            # Fall back to the product only for the part that was not reported.
            fallback = GPU_CLASSES.get(
                _PRODUCT_CLASSES.get(_normalise_product(entry.get("product")), "")
            )
            if fallback is not None:
                memory = memory or fallback.memory_bytes
                capability = capability or fallback.capability
        if memory == 0 or capability is None:
            # One device nobody can describe makes the whole stamp undescribed: a host may
            # land on it, so admitting on the others' numbers would be admitting on luck.
            weakest_memory, weakest_capability = None, None
            described = -1
            break
        weakest_memory = memory if weakest_memory is None else min(weakest_memory, memory)
        weakest_capability = (
            capability if weakest_capability is None else min(weakest_capability, capability)
        )

    if described <= 0:
        weakest_memory, weakest_capability = None, None

    return StampCapacity(
        allocatable_gpus=allocatable,
        foreign_requested_gpus=max(0, requested - fabric_requested),
        max_gpus_per_node=_positive_int(capabilities.get("max_gpus_per_node")),
        weakest_memory_bytes=weakest_memory,
        weakest_capability=weakest_capability,
        region=stamp.region,
        last_heartbeat_at=stamp.last_heartbeat_at,
    )


# --- fit ------------------------------------------------------------------


@dataclass(frozen=True)
class Unfit:
    """Why a stamp cannot hold a deployment."""

    code: str
    details: dict[str, Any]


def check_fit(
    demand: Demand,
    capacity: StampCapacity,
    *,
    committed_gpus: int,
    require_live: bool,
    now: dt.datetime | None = None,
) -> Unfit | None:
    """Return why this stamp cannot hold this deployment, or ``None`` if it can.

    ``require_live`` is true only for automatic selection. An explicitly named stamp is not
    refused for a stale heartbeat: the caller chose it, a stamp that missed a few beats
    usually returns, and refusing would be wrong the moment it does. Automatic selection has
    other options, so it declines to guess.
    """
    if demand.region is not None:
        stamp_region = (capacity.region or "").strip().lower()
        if stamp_region != demand.region.lower():
            return Unfit("region_mismatch", {"stamp_region": capacity.region or None})

    if require_live:
        moment = now or dt.datetime.now(tz=dt.UTC)
        heartbeat = capacity.last_heartbeat_at
        if heartbeat is None:
            return Unfit("stamp_not_heartbeating", {"last_heartbeat_at": None})
        if heartbeat.tzinfo is None:
            # SQLite hands back naive datetimes; they are stored as UTC.
            heartbeat = heartbeat.replace(tzinfo=dt.UTC)
        if moment - heartbeat > LIVENESS_WINDOW:
            return Unfit(
                "stamp_not_heartbeating",
                {"last_heartbeat_at": heartbeat.isoformat()},
            )

    if capacity.allocatable_gpus == 0:
        return Unfit("stamp_reports_no_gpus", {"allocatable_gpus": 0})

    if 0 < capacity.max_gpus_per_node < demand.gpus_per_replica:
        # One replica cannot be split across nodes, so a stamp-wide total does not help.
        return Unfit(
            "gpu_count_exceeds_largest_node",
            {"max_gpus_per_node": capacity.max_gpus_per_node},
        )

    required = GPU_CLASSES.get(demand.gpu_class)
    if required is not None and capacity.describes_its_hardware:
        assert capacity.weakest_memory_bytes is not None
        assert capacity.weakest_capability is not None
        if (
            capacity.weakest_memory_bytes < required.minimum_memory_bytes
            or capacity.weakest_capability < required.capability_floor
        ):
            return Unfit(
                "gpu_class_not_satisfied",
                {
                    "required_memory_bytes": required.minimum_memory_bytes,
                    "required_compute_capability": _format_capability(
                        required.capability_floor
                    ),
                    "weakest_memory_bytes": capacity.weakest_memory_bytes,
                    "weakest_compute_capability": _format_capability(
                        capacity.weakest_capability
                    ),
                },
            )

    free = capacity.free_gpus(committed_gpus)
    if free < demand.total_gpus:
        return Unfit(
            "insufficient_free_gpus",
            {
                "free_gpus": free,
                "allocatable_gpus": capacity.allocatable_gpus,
                "committed_gpus": committed_gpus,
                "foreign_requested_gpus": capacity.foreign_requested_gpus,
            },
        )
    return None


def _format_capability(capability: tuple[int, int]) -> str:
    return f"{capability[0]}.{capability[1]}"


def class_was_verified(demand: Demand, capacity: StampCapacity) -> bool:
    """Whether the class requirement was actually judged.

    A stamp that reports nothing usable about its devices is admitted rather than refused:
    refusing would make upgrading every existing agent a prerequisite for placing anything,
    turning a safety improvement into an outage. It is recorded on the audit trail instead,
    so an unverified admission is visible rather than silent.
    """
    return demand.gpu_class in GPU_CLASSES and capacity.describes_its_hardware


# --- commitment -----------------------------------------------------------


async def committed_gpus(
    session: AsyncSession,
    *,
    stamp_id: uuid.UUID,
    exclude_deployment_id: uuid.UUID | None = None,
) -> int:
    """GPUs this stamp has already been told to run, from the control plane's own records.

    Elevated, because a managed stamp serves several accounts and its true commitment is the
    sum across all of them. Scoped to the caller it would report only that customer's share
    and admit a stamp another customer has already filled. Read-only, and the elevation ends
    before anything is written.
    """
    conditions = [
        DeploymentPlacement.stamp_id == stamp_id,
        Deployment.deleted_at.is_(None),
        # A terminating placement is on its way out; counting it would refuse the
        # replacement of a deployment that is being replaced.
        DeploymentPlacement.status != "terminating",
    ]
    if exclude_deployment_id is not None:
        # The deployment being placed or resized is measured by its new demand, not by
        # itself.
        conditions.append(DeploymentPlacement.deployment_id != exclude_deployment_id)

    async with elevated(session):
        rows = await session.execute(
            select(Deployment.desired_spec)
            .join(
                DeploymentPlacement,
                (DeploymentPlacement.deployment_id == Deployment.id)
                & (DeploymentPlacement.account_id == Deployment.account_id),
            )
            .where(*conditions)
        )
        specs = list(rows.scalars().all())

    total = 0
    for spec in specs:
        placed = demand_of(spec if isinstance(spec, dict) else {})
        total += placed.total_gpus
    return total


async def candidate_stamps(
    session: AsyncSession, *, account_id: uuid.UUID, system_account_id: uuid.UUID | None
) -> list[InferenceStamp]:
    """Stamps this account could conceivably use: its own, plus managed capacity.

    Elevated for the same reason the single-stamp lookup is: managed capacity belongs to the
    Fabric system account, so a customer resolving it is reading another account's rows by
    design. Whether the caller may actually use each one is decided by the caller, which
    still applies the entitlement and ownership checks an explicit stamp passes.
    """
    owners = [account_id]
    if system_account_id is not None and system_account_id != account_id:
        owners.append(system_account_id)

    async with elevated(session):
        rows = await session.execute(
            select(InferenceStamp)
            .where(
                InferenceStamp.revoked_at.is_(None),
                InferenceStamp.account_id.in_(owners),
            )
            .order_by(InferenceStamp.created_at)
        )
        return list(rows.scalars().all())
