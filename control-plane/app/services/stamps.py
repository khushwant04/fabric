"""Inference-stamp enrollment and synchronization services.

Account ownership is always derived from the enrollment record or the presented
machine credential, never from the request payload.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.config import get_settings
from app.core.credentials import (
    PREFIX_AGENT,
    PREFIX_ENROLLMENT,
    PREFIX_TELEMETRY,
    generate_credential,
    parse_credential,
    verify_credential,
)
from app.core.errors import BadRequest, Forbidden, NotFound, Unauthorized
from app.core.timeutil import is_expired, utc_now
from app.models import (
    MODE_BYOI,
    MODE_MANAGED,
    Account,
    Deployment,
    DeploymentPlacement,
    DeploymentStatus,
    InferenceStamp,
    StampCredential,
    StampEnrollmentToken,
    TelemetryCredential,
)
from app.schemas import (
    DesiredDeployment,
    DesiredStateResponse,
    StampCapabilities,
)
from app.services.audit import publish_outbox, record_audit


async def create_enrollment_token(
    session: AsyncSession,
    *,
    account: Account,
    allowed_mode: str,
    expires_in_minutes: int,
    actor_user_id: uuid.UUID | None,
) -> tuple[StampEnrollmentToken, str]:
    """Create a single-use, account-bound enrollment token."""
    if allowed_mode == MODE_MANAGED and not account.is_system:
        raise Forbidden(
            "managed_enrollment_not_permitted",
            "Managed stamps may only be enrolled under the Fabric system account",
        )

    settings = get_settings()
    generated = generate_credential(settings.credential_pepper, PREFIX_ENROLLMENT)
    record = StampEnrollmentToken(
        id=generated.credential_id,
        account_id=account.id,
        token_prefix=generated.display_prefix,
        token_verifier=generated.verifier,
        allowed_mode=allowed_mode,
        expires_at=dt.datetime.now(tz=dt.UTC) + dt.timedelta(minutes=expires_in_minutes),
        created_by_user_id=actor_user_id,
    )
    session.add(record)
    await session.flush()
    await record_audit(
        session,
        account_id=account.id,
        actor_type="user",
        actor_id=str(actor_user_id) if actor_user_id else None,
        action="stamp_enrollment_token.created",
        resource_type="stamp_enrollment_token",
        resource_id=str(record.id),
        metadata={"allowed_mode": allowed_mode},
    )
    return record, generated.token


async def _consume_enrollment_token(
    session: AsyncSession, raw_token: str
) -> StampEnrollmentToken:
    """Verify and atomically claim a one-time enrollment token.

    Claiming uses a conditional ``UPDATE ... WHERE used_at IS NULL`` so two
    concurrent enrollments cannot both pass the single-use check: the losing
    transaction updates zero rows and is rejected.
    """
    parsed = parse_credential(raw_token, expected_prefix=PREFIX_ENROLLMENT)
    if parsed is None:
        raise Unauthorized("invalid_enrollment_token", "Enrollment token is malformed")
    _prefix, token_id, secret = parsed

    record = (
        await session.execute(
            select(StampEnrollmentToken).where(StampEnrollmentToken.id == token_id)
        )
    ).scalar_one_or_none()
    if record is None:
        raise Unauthorized("invalid_enrollment_token", "Enrollment token is not recognized")

    settings = get_settings()
    if not verify_credential(
        settings.credential_pepper, token_id, secret, record.token_verifier
    ):
        raise Unauthorized("invalid_enrollment_token", "Enrollment token is not recognized")

    now = utc_now()
    if record.revoked_at is not None:
        raise Unauthorized("enrollment_token_revoked", "Enrollment token has been revoked")
    if record.used_at is not None:
        raise Unauthorized("enrollment_token_used", "Enrollment token has already been used")
    if is_expired(record.expires_at, now=now):
        raise Unauthorized("enrollment_token_expired", "Enrollment token has expired")

    claimed = await session.execute(
        update(StampEnrollmentToken)
        .where(
            StampEnrollmentToken.id == record.id,
            StampEnrollmentToken.used_at.is_(None),
            StampEnrollmentToken.revoked_at.is_(None),
        )
        .values(used_at=now)
    )
    if claimed.rowcount != 1:
        # A concurrent request claimed the same token first.
        raise Unauthorized("enrollment_token_used", "Enrollment token has already been used")
    return record


async def enroll_stamp(
    session: AsyncSession,
    *,
    raw_token: str,
    name: str,
    capabilities: StampCapabilities,
) -> tuple[InferenceStamp, str, str]:
    """Register a stamp and mint separate agent and telemetry credentials."""
    token = await _consume_enrollment_token(session, raw_token)
    settings = get_settings()

    stamp = InferenceStamp(
        account_id=token.account_id,
        name=name,
        mode=token.allowed_mode,
        orchestrator=capabilities.orchestrator,
        region=capabilities.region,
        status="registered",
        capabilities=capabilities.model_dump(mode="json"),
        last_heartbeat_at=dt.datetime.now(tz=dt.UTC),
    )
    session.add(stamp)
    await session.flush()

    agent = generate_credential(settings.credential_pepper, PREFIX_AGENT)
    telemetry = generate_credential(settings.credential_pepper, PREFIX_TELEMETRY)
    session.add(
        StampCredential(
            id=agent.credential_id,
            account_id=stamp.account_id,
            stamp_id=stamp.id,
            credential_prefix=agent.display_prefix,
            credential_verifier=agent.verifier,
            scopes=sorted(scope_defs.AGENT_SCOPES),
        )
    )
    session.add(
        TelemetryCredential(
            id=telemetry.credential_id,
            account_id=stamp.account_id,
            stamp_id=stamp.id,
            credential_prefix=telemetry.display_prefix,
            credential_verifier=telemetry.verifier,
            scopes=sorted(scope_defs.TELEMETRY_SCOPES),
        )
    )
    await session.flush()

    await record_audit(
        session,
        account_id=stamp.account_id,
        actor_type="stamp",
        actor_id=str(stamp.id),
        action="stamp.enrolled",
        resource_type="inference_stamp",
        resource_id=str(stamp.id),
        metadata={"mode": stamp.mode, "orchestrator": stamp.orchestrator},
    )
    await publish_outbox(
        session,
        account_id=stamp.account_id,
        event_type="stamp.enrolled",
        aggregate_type="inference_stamp",
        aggregate_id=str(stamp.id),
        payload={"mode": stamp.mode},
    )
    return stamp, agent.token, telemetry.token


async def list_stamps(session: AsyncSession, account_id: uuid.UUID) -> list[InferenceStamp]:
    rows = await session.execute(
        select(InferenceStamp)
        .where(InferenceStamp.account_id == account_id)
        .order_by(InferenceStamp.created_at.desc())
    )
    return list(rows.scalars().all())


async def get_stamp(session: AsyncSession, stamp_id: uuid.UUID) -> InferenceStamp:
    stamp = (
        await session.execute(select(InferenceStamp).where(InferenceStamp.id == stamp_id))
    ).scalar_one_or_none()
    if stamp is None:
        raise NotFound("stamp_not_found", "Inference stamp does not exist")
    if stamp.revoked_at is not None:
        raise Forbidden("stamp_revoked", "Inference stamp has been revoked")
    return stamp


async def revoke_stamp(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    stamp_id: uuid.UUID,
    actor_type: str,
    actor_id: str | None,
) -> InferenceStamp:
    """Revoke a stamp and both of its machine credentials.

    Revocation stops further synchronization: heartbeat, capability refresh,
    desired-state reads, and status writes are all denied afterwards. Workloads
    already running in a BYOI cluster remain under customer cluster control.
    """
    stamp = (
        await session.execute(
            select(InferenceStamp).where(
                InferenceStamp.id == stamp_id, InferenceStamp.account_id == account_id
            )
        )
    ).scalar_one_or_none()
    if stamp is None:
        raise NotFound("stamp_not_found", "Inference stamp does not exist")

    if stamp.revoked_at is None:
        now = dt.datetime.now(tz=dt.UTC)
        stamp.revoked_at = now
        stamp.status = "revoked"
        for model in (StampCredential, TelemetryCredential):
            await session.execute(
                update(model)
                .where(
                    model.account_id == account_id,
                    model.stamp_id == stamp_id,
                    model.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
        await record_audit(
            session,
            account_id=account_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="stamp.revoked",
            resource_type="inference_stamp",
            resource_id=str(stamp.id),
        )
        await publish_outbox(
            session,
            account_id=account_id,
            event_type="stamp.revoked",
            aggregate_type="inference_stamp",
            aggregate_id=str(stamp.id),
            payload={"mode": stamp.mode},
        )
    await session.flush()
    return stamp


async def revoke_enrollment_token(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    token_id: uuid.UUID,
    actor_type: str,
    actor_id: str | None,
) -> StampEnrollmentToken:
    """Revoke an unused enrollment token so it can no longer register a stamp."""
    record = (
        await session.execute(
            select(StampEnrollmentToken).where(
                StampEnrollmentToken.id == token_id,
                StampEnrollmentToken.account_id == account_id,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise NotFound(
            "enrollment_token_not_found", "Stamp enrollment token does not exist"
        )
    if record.revoked_at is None:
        record.revoked_at = dt.datetime.now(tz=dt.UTC)
        await record_audit(
            session,
            account_id=account_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="stamp_enrollment_token.revoked",
            resource_type="stamp_enrollment_token",
            resource_id=str(record.id),
        )
    await session.flush()
    return record


async def record_heartbeat(
    session: AsyncSession,
    *,
    stamp_id: uuid.UUID,
    capabilities: StampCapabilities | None,
) -> dt.datetime:
    """Update liveness and optionally refresh bounded capability data."""
    stamp = await get_stamp(session, stamp_id)
    now = dt.datetime.now(tz=dt.UTC)
    stamp.last_heartbeat_at = now
    if capabilities is not None:
        stamp.capabilities = capabilities.model_dump(mode="json")
        stamp.orchestrator = capabilities.orchestrator
        stamp.region = capabilities.region
    if stamp.status == "registered":
        stamp.status = "active"
    await session.flush()
    return now


async def build_desired_state(
    session: AsyncSession, *, stamp_id: uuid.UUID, after_generation: int
) -> DesiredStateResponse:
    """Return assignments for this stamp newer than the acknowledged generation."""
    await get_stamp(session, stamp_id)
    rows = await session.execute(
        select(DeploymentPlacement, Deployment)
        .join(
            Deployment,
            (Deployment.id == DeploymentPlacement.deployment_id)
            & (Deployment.account_id == DeploymentPlacement.account_id),
        )
        .where(
            DeploymentPlacement.stamp_id == stamp_id,
            DeploymentPlacement.desired_generation > after_generation,
        )
        .order_by(DeploymentPlacement.desired_generation)
    )

    deployments: list[DesiredDeployment] = []
    max_generation = after_generation
    for placement, deployment in rows.all():
        deployments.append(
            DesiredDeployment(
                deployment_id=deployment.id,
                # From the placement, which is the ownership anchor: a managed
                # stamp's assignments belong to different customer accounts.
                account_id=placement.account_id,
                name=deployment.name,
                model_alias=deployment.model_alias,
                desired_generation=placement.desired_generation,
                spec=deployment.desired_spec,
                deleted=deployment.deleted_at is not None,
            )
        )
        max_generation = max(max_generation, placement.desired_generation)

    return DesiredStateResponse(
        stamp_id=stamp_id, max_generation=max_generation, deployments=deployments
    )


async def report_status(
    session: AsyncSession,
    *,
    stamp_id: uuid.UUID,
    deployment_id: uuid.UUID,
    observed_generation: int | None,
    phase: str,
    ready_replicas: int,
    unavailable_replicas: int,
    endpoint: str | None,
    conditions: list[dict],
) -> dt.datetime:
    """Persist observed status for an existing placement.

    ``stamp_id`` comes from the authenticated credential. The placement is the
    ownership anchor: a managed stamp owned by the Fabric system account reports
    status for a deployment owned by a customer account, so the owning account is
    read from the placement rather than from the reporting credential. Ownership
    is still never taken from the request body.
    """
    stamp = await get_stamp(session, stamp_id)

    placement = (
        await session.execute(
            select(DeploymentPlacement).where(
                DeploymentPlacement.stamp_id == stamp_id,
                DeploymentPlacement.deployment_id == deployment_id,
            )
        )
    ).scalar_one_or_none()
    if placement is None:
        raise BadRequest(
            "placement_not_found",
            "Deployment is not assigned to this stamp",
        )

    if stamp.mode == MODE_BYOI:
        if placement.account_id != stamp.account_id:
            raise Forbidden("placement_not_available", "Placement does not belong to this stamp")
    elif stamp.mode != MODE_MANAGED:
        # Unknown ownership modes fail closed.
        raise Forbidden("unsupported_stamp_mode", "Stamp ownership mode is not supported")

    account_id = placement.account_id
    now = dt.datetime.now(tz=dt.UTC)
    record = (
        await session.execute(
            select(DeploymentStatus).where(
                DeploymentStatus.account_id == account_id,
                DeploymentStatus.deployment_id == deployment_id,
                DeploymentStatus.stamp_id == stamp_id,
            )
        )
    ).scalar_one_or_none()

    if record is None:
        record = DeploymentStatus(
            account_id=account_id,
            deployment_id=deployment_id,
            stamp_id=stamp_id,
        )
        session.add(record)

    record.observed_generation = observed_generation
    record.phase = phase
    record.ready_replicas = ready_replicas
    record.unavailable_replicas = unavailable_replicas
    record.endpoint = endpoint
    record.conditions = conditions
    record.reported_at = now

    if observed_generation is not None:
        placement.observed_generation = observed_generation
        if observed_generation >= placement.desired_generation and phase == "ready":
            placement.status = "ready"

    deployment = (
        await session.execute(
            select(Deployment).where(
                Deployment.id == deployment_id, Deployment.account_id == account_id
            )
        )
    ).scalar_one_or_none()
    if deployment is not None and deployment.deleted_at is None:
        deployment.status = phase

    await session.flush()
    return now
