"""Deployment intent and placement services.

Placement is the only path that crosses account ownership: a customer-owned
deployment may target a managed stamp owned by the protected Fabric system
account. Every branch is explicit and fails closed.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequest, Conflict, Forbidden, NotFound
from app.core.platform import is_supported_orchestrator
from app.core.tenancy import elevated
from app.models import (
    MODE_BYOI,
    MODE_MANAGED,
    Account,
    Deployment,
    DeploymentPlacement,
    DeploymentStatus,
    InferenceStamp,
)
from app.services import placement
from app.services.accounts import get_account, get_system_account
from app.services.audit import publish_outbox, record_audit


async def create_deployment(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    name: str,
    model_alias: str,
    spec: dict[str, Any],
    actor_principal_id: uuid.UUID | None,
) -> Deployment:
    """Record new deployment intent at generation 1."""
    deployment = Deployment(
        account_id=account_id,
        name=name,
        model_alias=model_alias,
        desired_spec=spec,
        generation=1,
        status="pending",
        created_by_principal_id=actor_principal_id,
    )
    session.add(deployment)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("deployment_name_taken", "Deployment name is already in use") from exc

    await record_audit(
        session,
        account_id=account_id,
        actor_type="principal",
        actor_id=str(actor_principal_id) if actor_principal_id else None,
        action="deployment.created",
        resource_type="deployment",
        resource_id=str(deployment.id),
        metadata={"name": name, "generation": deployment.generation},
    )
    await publish_outbox(
        session,
        account_id=account_id,
        event_type="deployment.created",
        aggregate_type="deployment",
        aggregate_id=str(deployment.id),
        payload={"generation": deployment.generation},
    )
    return deployment


async def list_deployments(session: AsyncSession, account_id: uuid.UUID) -> list[Deployment]:
    rows = await session.execute(
        select(Deployment)
        .where(Deployment.account_id == account_id, Deployment.deleted_at.is_(None))
        .order_by(Deployment.created_at.desc())
    )
    return list(rows.scalars().all())


async def get_deployment(
    session: AsyncSession,
    account_id: uuid.UUID,
    deployment_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Deployment:
    statement = select(Deployment).where(
        Deployment.id == deployment_id,
        Deployment.account_id == account_id,
        Deployment.deleted_at.is_(None),
    )
    if for_update:
        # Placement and spec replacement both evaluate capacity from this row. Locking it
        # first gives them one shared order: either placement commits and the update sees the
        # new assignment, or the update commits and placement evaluates the new spec. Without
        # this, each can see the old world and jointly create an oversized assignment.
        statement = statement.with_for_update().execution_options(populate_existing=True)
    deployment = (await session.execute(statement)).scalar_one_or_none()
    if deployment is None:
        raise NotFound("deployment_not_found", "Deployment does not exist")
    return deployment


async def _next_stamp_generation(session: AsyncSession, stamp_id: uuid.UUID, floor: int) -> int:
    """The next delivery generation for a stamp, ahead of anything it has been sent.

    An agent acknowledges one number for the whole stamp, so the generations it is sent
    have to increase across the stamp and not merely within one deployment. Taking the
    number from the deployment's own version broke that: a stamp that had acknowledged
    generation two for one deployment would never be told about a newly placed deployment
    whose first version is one, because it sits below the watermark. The placement existed,
    the API reported it assigned, and nothing was ever served.

    Elevated because "across the stamp" means across every account on it. A managed stamp
    serves several, and row-level security would otherwise scope this maximum to the caller,
    computing a watermark below what the stamp has already acknowledged for somebody else —
    reintroducing exactly the silent non-delivery the watermark exists to prevent. Read-only,
    and the elevation ends before the placement is written.
    """
    async with elevated(session):
        current = (
            await session.execute(
                select(
                    func.coalesce(func.max(DeploymentPlacement.desired_generation), 0)
                ).where(DeploymentPlacement.stamp_id == stamp_id)
            )
        ).scalar_one()
    return max(floor, int(current) + 1)


async def _bump_placements(session: AsyncSession, deployment: Deployment) -> None:
    placements = (
        (
            await session.execute(
                select(DeploymentPlacement).where(
                    DeploymentPlacement.account_id == deployment.account_id,
                    DeploymentPlacement.deployment_id == deployment.id,
                )
            )
        )
        .scalars()
        .all()
    )
    # Per stamp, because each stamp tracks its own watermark and they are not in step.
    for record in placements:
        record.desired_generation = await _next_stamp_generation(
            session, record.stamp_id, deployment.generation
        )
        record.status = "assigned"


async def _refuse_placements_that_no_longer_fit(
    session: AsyncSession,
    *,
    account: Account,
    deployment: Deployment,
    spec: dict[str, Any],
) -> dict[str, Any] | None:
    """Raise unless every stamp holding this deployment can still hold it under ``spec``.

    Checked before the spec is stored, so a refused update leaves the deployment exactly as
    it was rather than depending on a rollback. Returns what the check established for the audit
    trail, or ``None`` when no stamp was consulted at all — an edit asking for no more than
    before, or one whose every stamp is revoked, has not been checked against hardware, and
    recording it as verified would put a claim on the audit trail that nothing established.

    Only an edit that asks for *more* is checked. Judging every edit against the stamp's
    current occupancy would mean that once a stamp is oversubscribed — a foreign workload
    appeared, a rollout is in flight, a device plugin restarted — even lowering ``replicas``
    is refused, and shrinking is the one remedy its owner has.
    """
    previous = placement.demand_of(deployment.desired_spec)
    demand = placement.demand_of(spec, region=None)

    if demand.gpu_class != previous.gpu_class:
        # Validated only when it changes. A deployment created before a class was named must
        # stay editable, which is the whole reason creation does not validate the catalogue;
        # setting a class nobody recognises is still refused.
        await _demand_for_spec(spec)

    if not demand.exceeds(previous):
        return None

    existing = (
        (
            await session.execute(
                select(DeploymentPlacement)
                .where(
                    DeploymentPlacement.account_id == deployment.account_id,
                    DeploymentPlacement.deployment_id == deployment.id,
                    DeploymentPlacement.status != "terminating",
                )
                # Ordered so every caller takes these stamp locks in the same sequence. Two
                # edits of two deployments sharing two stamps would otherwise acquire them in
                # opposite orders and deadlock.
                .order_by(DeploymentPlacement.stamp_id)
            )
        )
        .scalars()
        .all()
    )
    if not existing:
        return None

    # Both start optimistic and are only ever weakened, and neither is returned unless a stamp
    # was actually consulted: a revoked stamp is skipped, and an edit that skipped every one of
    # them has verified nothing. Reporting True there would be the same inversion this pair of
    # fields exists to prevent.
    consulted = False
    verified = True
    claims_measured = True
    for record in existing:
        async with elevated(session):
            stamp = (
                await session.execute(
                    select(InferenceStamp).where(InferenceStamp.id == record.stamp_id)
                )
            ).scalar_one_or_none()
        if stamp is None or stamp.revoked_at is not None:
            # A revoked stamp serves nothing either way, so its capacity is not a reason to
            # refuse an edit. Withdrawing the placement is a separate action.
            continue
        capacity = await _refuse_if_stamp_cannot_fit(
            session,
            account=account,
            stamp=stamp,
            deployment_id=deployment.id,
            demand=demand,
            require_live=False,
        )
        consulted = True
        verified = verified and placement.class_was_verified(demand, capacity)
        claims_measured = claims_measured and capacity.claims_measured
    if not consulted:
        return None
    return {"gpu_class_verified": verified, "gpu_claims_measured": claims_measured}


async def update_deployment(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    deployment_id: uuid.UUID,
    spec: dict[str, Any],
    actor_principal_id: uuid.UUID | None,
) -> Deployment:
    """Replace the desired spec and advance the generation monotonically.

    The new spec is checked against every stamp already holding this deployment before
    anything is written. Raising ``replicas`` or ``gpu_count`` overcommits a stamp exactly as
    an oversized placement does, and refusing at reconcile time is the failure ADR 0013
    exists to move to the API.
    """
    account = await get_account(session, account_id)
    deployment = await get_deployment(
        session, account_id, deployment_id, for_update=True
    )
    checked = await _refuse_placements_that_no_longer_fit(
        session, account=account, deployment=deployment, spec=spec
    )
    deployment.desired_spec = spec
    deployment.generation += 1
    deployment.status = "pending"
    await _bump_placements(session, deployment)
    await session.flush()

    await record_audit(
        session,
        account_id=account_id,
        actor_type="principal",
        actor_id=str(actor_principal_id) if actor_principal_id else None,
        action="deployment.updated",
        resource_type="deployment",
        resource_id=str(deployment.id),
        metadata={
            "generation": deployment.generation,
            # Whether any stamp was consulted, and if so whether its hardware could answer the
            # class requirement. Separate fields because "not checked" and "checked and
            # unverifiable" are different facts, and recording an unchecked edit as verified
            # would put a claim on the audit trail that nothing established.
            "capacity_checked": checked is not None,
            **(checked or {}),
        },
    )
    await publish_outbox(
        session,
        account_id=account_id,
        event_type="deployment.updated",
        aggregate_type="deployment",
        aggregate_id=str(deployment.id),
        payload={"generation": deployment.generation},
    )
    return deployment


async def delete_deployment(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    deployment_id: uuid.UUID,
    actor_principal_id: uuid.UUID | None,
) -> None:
    """Soft-delete intent and instruct stamps to remove the workload."""
    deployment = await get_deployment(
        session, account_id, deployment_id, for_update=True
    )
    deployment.deleted_at = dt.datetime.now(tz=dt.UTC)
    deployment.generation += 1
    deployment.status = "terminating"
    await _bump_placements(session, deployment)
    await session.execute(
        update(DeploymentPlacement)
        .where(
            DeploymentPlacement.account_id == account_id,
            DeploymentPlacement.deployment_id == deployment.id,
        )
        .values(status="terminating")
    )
    await session.flush()

    await record_audit(
        session,
        account_id=account_id,
        actor_type="principal",
        actor_id=str(actor_principal_id) if actor_principal_id else None,
        action="deployment.deleted",
        resource_type="deployment",
        resource_id=str(deployment.id),
        metadata={"generation": deployment.generation},
    )
    await publish_outbox(
        session,
        account_id=account_id,
        event_type="deployment.deleted",
        aggregate_type="deployment",
        aggregate_id=str(deployment.id),
        payload={"generation": deployment.generation},
    )


def _stamp_unavailable(
    stamp: InferenceStamp, *, account: Account, system_account: Account | None
) -> Forbidden | None:
    """Why this account may not deploy onto this stamp, or ``None`` if it may.

    Returned rather than raised because automatic selection has to ask the same question of
    every candidate and skip the ones that answer badly, while an explicitly named stamp must
    still fail with these exact codes. One implementation, two callers, so selection cannot
    drift into handing out capacity the entitlement checks protect.
    """
    if stamp.mode == MODE_BYOI:
        if stamp.account_id != account.id:
            return Forbidden("stamp_not_available", "Stamp belongs to a different account")
        return None

    if stamp.mode == MODE_MANAGED:
        if system_account is None or stamp.account_id != system_account.id:
            return Forbidden("stamp_not_available", "Managed stamp ownership is invalid")
        if not is_supported_orchestrator(stamp.orchestrator):
            return Forbidden(
                "unsupported_orchestrator",
                "Managed stamp runs an unsupported Kubernetes distribution",
                orchestrator=stamp.orchestrator,
            )
        if not account.managed_capacity_enabled:
            return Forbidden(
                "managed_capacity_not_enabled",
                "Account is not entitled to managed capacity",
            )
        return None

    # Unknown modes fail closed.
    return Forbidden("unsupported_stamp_mode", "Stamp ownership mode is not supported")


async def _authorize_stamp_for_account(
    session: AsyncSession, *, account: Account, stamp_id: uuid.UUID
) -> InferenceStamp:
    """Return the stamp only when this account may deploy onto it."""
    # Elevated for the lookup alone. Managed capacity is owned by the Fabric system
    # account, so a customer resolving a shared stamp is reading another account's
    # row by design; scoping this read to the caller would make managed placement
    # impossible. Whether the caller may use the stamp is decided below, and the
    # elevation is dropped before anything is written.
    async with elevated(session):
        stamp = (
            await session.execute(select(InferenceStamp).where(InferenceStamp.id == stamp_id))
        ).scalar_one_or_none()
    if stamp is None or stamp.revoked_at is not None:
        raise NotFound("stamp_not_found", "Inference stamp does not exist")

    system_account = None
    if stamp.mode == MODE_MANAGED:
        system_account = await get_system_account(session)
    refusal = _stamp_unavailable(stamp, account=account, system_account=system_account)
    if refusal is not None:
        raise refusal
    return stamp


async def _demand_for_spec(
    spec: dict[str, Any] | None, *, region: str | None = None
) -> placement.Demand:
    """Read demand from a spec, refusing a class nobody can evaluate.

    A class outside the catalogue is a property of the deployment rather than of any stamp,
    so it is answered before stamps are considered, and it is a bad request rather than a
    conflict: no amount of capacity makes it placeable.
    """
    demand = placement.demand_of(spec, region=region)
    if demand.gpu_class not in placement.GPU_CLASSES:
        raise BadRequest(
            "unknown_gpu_class",
            "Deployment asks for a GPU class this platform does not recognise",
            gpu_class=demand.gpu_class,
            known_gpu_classes=sorted(placement.GPU_CLASSES),
        )
    return demand


async def _refuse_if_stamp_cannot_fit(
    session: AsyncSession,
    *,
    account: Account,
    stamp: InferenceStamp,
    deployment_id: uuid.UUID,
    demand: placement.Demand,
    require_live: bool,
) -> placement.StampCapacity:
    """Raise unless this stamp can hold this demand, naming what is short.

    Takes the stamp's row first, so the capacity this decides on cannot change under it while
    another placement is being admitted.
    """
    locked = await placement.lock_stamp(session, stamp_id=stamp.id)
    if locked is None:
        # Removed between authorization and the lock.
        raise NotFound("stamp_not_found", "Inference stamp does not exist")
    if locked.revoked_at is not None:
        # Revocation writes this same row, so it now serializes against admission rather than
        # racing it. A stamp revoked while this decision was queued stops fetching desired
        # state, so assigning to it would produce a placement nothing will ever serve.
        raise NotFound("stamp_not_found", "Inference stamp does not exist")
    raw_capacity = placement.read_capacity(locked)
    committed = await placement.committed_gpus(session, stamp_id=stamp.id)
    subject_key = str(deployment_id).lower()
    replaceable = committed.pop(subject_key, 0)
    capacity = placement.without_deployment(
        raw_capacity,
        deployment_id,
        replaceable_gpus=replaceable,
    )
    unfit = placement.check_fit(
        demand,
        capacity,
        committed_gpus=committed,
        require_live=require_live,
    )
    if unfit is not None:
        raise Conflict(
            "stamp_cannot_fit_deployment",
            "Stamp cannot host this deployment as specified",
            reason=unfit.code,
            stamp_id=str(stamp.id),
            demand=demand.as_details(),
            **unfit.details,
            # Occupancy only for the caller's own hardware: on shared managed capacity these
            # are sums across every account on the stamp.
            **(unfit.occupancy if stamp.account_id == account.id else {}),
        )
    return capacity


#: How many candidate refusals an error carries. Enough to diagnose, bounded so a large fleet
#: cannot turn one rejected placement into an unbounded response body.
_MAX_REPORTED_CANDIDATES = 20


async def _select_stamp(
    session: AsyncSession,
    *,
    account: Account,
    deployment: Deployment,
    demand: placement.Demand,
) -> InferenceStamp:
    """Choose the least loaded stamp this account may use that can hold the deployment.

    Preference order is deliberate: the account's own hardware before Fabric's managed
    capacity, because a customer's cluster is already paid for and managed capacity is
    billed; then the most free GPUs, which is what "least loaded" means when GPUs are the
    only resource modelled; then stamp id, so the same request does not wander between
    equally good stamps on retry.
    """
    system_account = await get_system_account(session)
    candidates = await placement.candidate_stamps(
        session,
        account_id=account.id,
        system_account_id=system_account.id if system_account else None,
    )

    fitting: list[tuple[int, int, str, InferenceStamp]] = []
    refusals: list[dict[str, Any]] = []

    def refusal_entry(stamp: InferenceStamp, unfit: placement.Unfit) -> dict[str, Any]:
        # A stamp the caller does not own is reported without its id or its occupancy: the
        # reason is what a caller can act on, while the id of managed capacity it is not
        # entitled to, and how full that capacity is, are not its business.
        own = stamp.account_id == account.id
        entry: dict[str, Any] = {"reason": unfit.code, **unfit.details}
        if own:
            entry["stamp_id"] = str(stamp.id)
            entry.update(unfit.occupancy)
        return entry

    for stamp in candidates:
        refusal = _stamp_unavailable(stamp, account=account, system_account=system_account)
        if refusal is not None:
            refusals.append(refusal_entry(stamp, placement.Unfit(refusal.code, {})))
            continue

        raw_capacity = placement.read_capacity(stamp)
        committed = await placement.committed_gpus(session, stamp_id=stamp.id)
        subject_key = str(deployment.id).lower()
        replaceable = committed.pop(subject_key, 0)
        capacity = placement.without_deployment(
            raw_capacity,
            deployment.id,
            replaceable_gpus=replaceable,
        )
        unfit = placement.check_fit(
            demand,
            capacity,
            committed_gpus=committed,
            require_live=True,
        )
        if unfit is not None:
            refusals.append(refusal_entry(stamp, unfit))
            continue

        fitting.append(
            (
                0 if stamp.account_id == account.id else 1,
                -capacity.free_gpus(committed),
                str(stamp.id),
                stamp,
            )
        )

    if not fitting:
        own = [entry for entry in refusals if "stamp_id" in entry]
        # Stamps the caller does not own are reported as the *set* of reasons managed capacity
        # gave, with no ids and no count: a count would let a caller size Fabric's fleet, and a
        # per-stamp entry would let it watch that fleet's state.
        shared = sorted({entry["reason"] for entry in refusals if "stamp_id" not in entry})
        raise Conflict(
            "no_stamp_fits_deployment",
            "No stamp available to this account can host this deployment",
            demand=demand.as_details(),
            candidates=own[:_MAX_REPORTED_CANDIDATES],
            candidates_considered=len(own),
            managed_capacity_reasons=shared,
        )

    fitting.sort(key=lambda entry: entry[:3])
    return fitting[0][3]


async def create_placement(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    deployment_id: uuid.UUID,
    stamp_id: uuid.UUID | None,
    region: str | None = None,
    actor_principal_id: uuid.UUID | None,
) -> DeploymentPlacement:
    """Assign a deployment to a stamp that can actually hold it (ADR 0013).

    ``stamp_id`` omitted selects one. Given, it is authorized as before and then checked for
    fit, because an assignment a stamp cannot serve is not an assignment: before this the API
    answered ``201`` and the pod stayed ``Pending`` forever.
    """
    account = await get_account(session, account_id)
    deployment = await get_deployment(
        session, account_id, deployment_id, for_update=True
    )
    demand = await _demand_for_spec(deployment.desired_spec, region=region)

    selected = stamp_id is None
    if selected:
        stamp = await _select_stamp(
            session, account=account, deployment=deployment, demand=demand
        )
    else:
        stamp = await _authorize_stamp_for_account(session, account=account, stamp_id=stamp_id)

    existing = (
        await session.execute(
            select(DeploymentPlacement).where(
                DeploymentPlacement.account_id == account_id,
                DeploymentPlacement.deployment_id == deployment.id,
                DeploymentPlacement.stamp_id == stamp.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Already assigned here, so this is a re-advertisement rather than a claim on capacity:
        # it bumps the generation and nothing else. Re-checking fit would make replaying a
        # request fail once something else filled the stamp, and this deployment's demand was
        # admitted when it was placed — every path that can raise it since then checks again.
        existing.desired_generation = await _next_stamp_generation(
            session, stamp.id, deployment.generation
        )
        existing.status = "assigned"
        await session.flush()
        return existing

    # Re-checked under the stamp's own row lock, whether it was named or selected. Selection
    # filters on a read nobody was holding, so the winner is a candidate rather than a
    # decision until this passes.
    capacity = await _refuse_if_stamp_cannot_fit(
        session,
        account=account,
        stamp=stamp,
        deployment_id=deployment.id,
        demand=demand,
        # Liveness is a selection rule, not an admission rule: a named stamp is not refused
        # for a stale heartbeat, and a selected one already passed it. See check_fit.
        require_live=False,
    )

    # The first lookup happened before this transaction acquired the stamp lock. A concurrent
    # request for the same deployment and stamp may have waited on that lock, inserted, and
    # committed meanwhile. Read again under the lock so the second caller returns the existing
    # assignment instead of colliding with its uniqueness constraint.
    existing = (
        await session.execute(
            select(DeploymentPlacement).where(
                DeploymentPlacement.account_id == account_id,
                DeploymentPlacement.deployment_id == deployment.id,
                DeploymentPlacement.stamp_id == stamp.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.desired_generation = await _next_stamp_generation(
            session, stamp.id, deployment.generation
        )
        existing.status = "assigned"
        await session.flush()
        return existing

    # Named ``record`` rather than ``placement`` so it does not shadow the placement service
    # this function calls for fit and selection.
    record = DeploymentPlacement(
        account_id=account_id,
        deployment_id=deployment.id,
        stamp_id=stamp.id,
        desired_generation=await _next_stamp_generation(
            session, stamp.id, deployment.generation
        ),
        status="assigned",
    )
    session.add(record)
    await session.flush()

    await record_audit(
        session,
        account_id=account_id,
        actor_type="principal",
        actor_id=str(actor_principal_id) if actor_principal_id else None,
        action="placement.created",
        resource_type="placement",
        resource_id=str(record.id),
        metadata={
            "stamp_id": str(stamp.id),
            "stamp_mode": stamp.mode,
            # Whether the platform chose the stamp or the caller named it, and whether the
            # GPU class was actually judged: a stamp that describes none of its hardware is
            # admitted rather than refused, and that has to be visible rather than silent.
            "stamp_selected": selected,
            "gpu_class_verified": placement.class_was_verified(demand, capacity),
            # Whether the stamp had read its own pod claims. Without them free capacity comes
            # from placement rows alone, so an admission decided that way is not the same fact
            # as one decided against measured hardware.
            "gpu_claims_measured": capacity.claims_measured,
            "demand": demand.as_details(),
        },
    )
    await publish_outbox(
        session,
        account_id=account_id,
        event_type="placement.created",
        aggregate_type="placement",
        aggregate_id=str(record.id),
        payload={"deployment_id": str(deployment.id), "stamp_id": str(stamp.id)},
    )
    return record


async def list_placements(
    session: AsyncSession, account_id: uuid.UUID, deployment_id: uuid.UUID
) -> list[DeploymentPlacement]:
    rows = await session.execute(
        select(DeploymentPlacement)
        .where(
            DeploymentPlacement.account_id == account_id,
            DeploymentPlacement.deployment_id == deployment_id,
        )
        .order_by(DeploymentPlacement.created_at)
    )
    return list(rows.scalars().all())


async def list_deployment_status(
    session: AsyncSession, account_id: uuid.UUID, deployment_id: uuid.UUID
) -> list[DeploymentStatus]:
    rows = await session.execute(
        select(DeploymentStatus).where(
            DeploymentStatus.account_id == account_id,
            DeploymentStatus.deployment_id == deployment_id,
        )
    )
    return list(rows.scalars().all())
