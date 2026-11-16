"""Serving metrics: pushed by stamp agents, read as chart series by the console.

The write uses the stamp's agent credential and the status-write scope every agent
credential holds, like host reports. The reads use the same scope as reading the deployment
or stamp, are never cached, and answer ``Cache-Control: no-store`` (docs/serving-metrics.md).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.database import get_db_session
from app.core.security import (
    PrincipalContext,
    StampContext,
    account_scope,
    require_agent_credential,
    require_stamp_match,
)
from app.schemas_serving_metrics import (
    AccountMetricsResponse,
    DeploymentMetricsResponse,
    MetricReportRequest,
    MetricReportResponse,
    StampMetricsResponse,
    Window,
)
from app.services.deployments import get_deployment
from app.services.serving_metrics import (
    account_metrics,
    deployment_metrics,
    ingest_metrics,
    stamp_metrics,
)

router = APIRouter(tags=["metrics"])

_WINDOW = Query("1h", description="How far back to chart: 1h, 6h, 24h or 7d")


@router.post(
    "/v1/stamps/{stamp_id}/metrics",
    response_model=MetricReportResponse,
    summary="Report serving metrics for this stamp's deployments and GPUs",
)
async def report_metrics(
    payload: MetricReportRequest,
    stamp_id: uuid.UUID = Path(...),
    context: StampContext = Depends(require_agent_credential),
    session: AsyncSession = Depends(get_db_session),
) -> MetricReportResponse:
    """Merge one or more samples into the per-minute series.

    Deployments not placed on this stamp are ignored, never stored. Samples older than the
    seven-day retention or more than five minutes in the future are skipped.
    """
    require_stamp_match(context, stamp_id)
    context.require_scopes(scope_defs.STAMP_STATUS_WRITE)
    result = await ingest_metrics(session, stamp_id=context.stamp_id, report=payload)
    await session.commit()
    return MetricReportResponse(
        stamp_id=context.stamp_id,
        accepted_at=result.accepted_at,
        accepted_samples=result.accepted_samples,
        skipped_samples=result.skipped_samples,
        ignored_deployments=result.ignored_deployments,
    )


@router.get(
    "/v1/accounts/{account_id}/deployments/{deployment_id}/metrics",
    response_model=DeploymentMetricsResponse,
    summary="Read a deployment's serving metrics as chart series",
)
async def read_deployment_metrics(
    response: Response,
    deployment_id: uuid.UUID = Path(...),
    window: Window = _WINDOW,
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> DeploymentMetricsResponse:
    """Requests, errors, latency and TTFT percentiles, throughput, queue, KV cache and GPU
    use, one point per chart step. Not cached: a console polling it wants the newest minute."""
    # Confirms the deployment exists, is not deleted, and belongs to the token's account.
    await get_deployment(session, principal.account_id, deployment_id)
    result = await deployment_metrics(
        session, account_id=principal.account_id, deployment_id=deployment_id, window=window
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get(
    "/v1/accounts/{account_id}/stamps/{stamp_id}/metrics",
    response_model=StampMetricsResponse,
    summary="Read a stamp's GPU and traffic metrics as chart series",
)
async def read_stamp_metrics(
    response: Response,
    stamp_id: uuid.UUID = Path(...),
    window: Window = _WINDOW,
    principal: PrincipalContext = Depends(account_scope(scope_defs.STAMPS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> StampMetricsResponse:
    """Per-GPU utilisation and memory, and the stamp's totals. Not cached."""
    result = await stamp_metrics(
        session, account_id=principal.account_id, stamp_id=stamp_id, window=window
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get(
    "/v1/accounts/{account_id}/metrics",
    response_model=AccountMetricsResponse,
    summary="Read the account's request rate and throughput across deployments",
)
async def read_account_metrics(
    response: Response,
    window: Window = _WINDOW,
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> AccountMetricsResponse:
    """Every deployment together, for the overview's sparklines. Not cached."""
    result = await account_metrics(session, account_id=principal.account_id, window=window)
    response.headers["Cache-Control"] = "no-store"
    return result
