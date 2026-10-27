"""Data-zone catalogue and zone-aware inference route discovery (ADR 0017)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import data_zones
from app.core import scopes as scope_defs
from app.core.database import get_db_session
from app.core.errors import BadRequest
from app.core.security import PrincipalContext, account_scope
from app.schemas import (
    DataZoneCatalogResponse,
    DataZoneResponse,
    InferenceRouteResponse,
    InferenceRoutesResponse,
)
from app.services.routing import list_inference_routes

router = APIRouter(tags=["data-zones"])


@router.get(
    "/v1/data-zones",
    response_model=DataZoneCatalogResponse,
    summary="List data zones and the regions inside each",
)
async def catalogue() -> DataZoneCatalogResponse:
    """Public, like the signing keys: it is the same for every caller and holds no secrets.

    A client needs it before it holds any credential, to decide which zone to bind a key to.
    """
    return DataZoneCatalogResponse(
        levels=list(data_zones.RESIDENCY_LEVELS),
        zones=[DataZoneResponse(**zone) for zone in data_zones.catalogue()],
    )


@router.get(
    "/v1/accounts/{account_id}/inference-routes",
    response_model=InferenceRoutesResponse,
    summary="Find the stamp endpoints that serve a model, optionally within a data zone",
)
async def inference_routes(
    model: str = Query(min_length=1, max_length=200, description="Model alias or deployment id"),
    data_zone: str | None = Query(default=None, max_length=16),
    region: str | None = Query(default=None, max_length=64),
    include_ineligible: bool = Query(
        default=False,
        description="Also list placements whose stamp no longer satisfies residency",
    ),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> InferenceRoutesResponse:
    """Not cached: health is the point of the answer, and it changes between heartbeats."""
    zone = data_zones.normalise_zone(data_zone)
    if zone is not None and not data_zones.is_known_zone(zone):
        raise BadRequest(
            "unknown_data_zone",
            "Data zone is not in the catalogue",
            data_zone=zone,
            known_data_zones=sorted(data_zones.DATA_ZONES),
        )
    routes = await list_inference_routes(
        session,
        account_id=principal.account_id,
        model=model,
        data_zone=zone,
        region=region,
        include_ineligible=include_ineligible,
    )
    return InferenceRoutesResponse(
        model=model,
        data_zone=zone,
        region=data_zones.normalise_region(region),
        routes=[InferenceRouteResponse(**route) for route in routes],
    )
