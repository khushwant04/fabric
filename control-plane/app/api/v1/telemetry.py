"""Telemetry ingestion.

Authenticated by the write-only telemetry credential and nothing else. The agent
credential is stored in a different table with a different prefix, so presenting
it here fails authentication rather than being silently accepted.

Ownership is resolved from the credential and the placement, so a stamp can only
report usage for deployments actually assigned to it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.database import get_db_session
from app.core.security import StampContext, require_telemetry_credential
from app.schemas import (
    MetricsAcceptedResponse,
    MetricsReportRequest,
    UsageIngestRequest,
    UsageIngestResponse,
    UsageRejectionResponse,
)
from app.services.metrics import record_metrics
from app.services.stamps import get_stamp
from app.services.usage import ingest_usage

router = APIRouter(tags=["telemetry"])


@router.post(
    "/v1/telemetry/usage",
    response_model=UsageIngestResponse,
    summary="Report usage for deployments served by this stamp",
)
async def report_usage(
    payload: UsageIngestRequest,
    context: StampContext = Depends(require_telemetry_credential),
    session: AsyncSession = Depends(get_db_session),
) -> UsageIngestResponse:
    """Accept a batch of usage records from a stamp's collector.

    Records are independent: an unplaced or duplicate record does not discard the
    rest of the batch, because collectors retry whole batches and partial progress
    must survive.
    """
    context.require_scopes(scope_defs.TELEMETRY_WRITE)
    # A revoked stamp stops reporting, matching desired-state and status reads.
    await get_stamp(session, context.stamp_id)

    result = await ingest_usage(session, stamp_id=context.stamp_id, records=payload.records)
    await session.commit()

    return UsageIngestResponse(
        accepted=result.accepted,
        duplicates=result.duplicates,
        rejected=result.rejected,
        rejections=[
            UsageRejectionResponse(
                index=rejection.index,
                code=rejection.code,
                deployment_id=rejection.deployment_id,
            )
            for rejection in result.rejections
        ],
    )


@router.post(
    "/v1/telemetry/metrics",
    response_model=MetricsAcceptedResponse,
    summary="Report GPU and runtime metrics for this stamp",
)
async def report_metrics(
    payload: MetricsReportRequest,
    context: StampContext = Depends(require_telemetry_credential),
    session: AsyncSession = Depends(get_db_session),
) -> MetricsAcceptedResponse:
    """Accept a stamp's periodic sample of its devices and model host.

    Metrics are operational signal, not billing input, and they are deliberately not
    stored as rows: a time series belongs in a time-series store, and adding a table here
    would create one with no retention policy, no downsampling, and no query language.
    Fabric has no such store yet, so the samples are recorded on the stamp's heartbeat
    state and logged, which keeps the path real and gives a consumer something to read
    without pretending a database is a metrics system.
    """
    context.require_scopes(scope_defs.TELEMETRY_WRITE)
    # A revoked stamp stops reporting, matching usage and desired state.
    stamp = await get_stamp(session, context.stamp_id)

    recorded = await record_metrics(
        session,
        stamp=stamp,
        collected_at=payload.collected_at,
        gpus=[sample.model_dump() for sample in payload.gpus],
        runtime=payload.runtime.model_dump(),
    )
    await session.commit()

    return MetricsAcceptedResponse(
        stamp_id=context.stamp_id,
        gpus_recorded=recorded.gpus_recorded,
        runtime_recorded=recorded.runtime_recorded,
    )
