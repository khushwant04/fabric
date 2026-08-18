"""Version 1 API router aggregation."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    accounts,
    api_keys,
    deployments,
    identity_providers,
    service_principals,
    stamps,
    system,
    telemetry,
    tokens,
)

api_router = APIRouter()
api_router.include_router(system.router)
api_router.include_router(tokens.router)
api_router.include_router(accounts.router)
api_router.include_router(service_principals.router)
api_router.include_router(api_keys.router)
api_router.include_router(identity_providers.router)
api_router.include_router(deployments.router)
api_router.include_router(stamps.router)
api_router.include_router(telemetry.router)

__all__ = ["api_router"]
