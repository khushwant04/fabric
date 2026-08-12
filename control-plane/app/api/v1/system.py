"""Health, readiness, and JWKS endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.core.jwt_service import get_jwks

router = APIRouter(tags=["system"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe")
async def readyz(session: AsyncSession = Depends(get_db_session)) -> dict[str, str]:
    """Readiness requires a usable database connection."""
    await session.execute(text("SELECT 1"))
    return {"status": "ready"}


@router.get("/.well-known/jwks.json", summary="Fabric token signing keys")
async def jwks() -> dict[str, list[dict[str, Any]]]:
    """Publish active and retiring public keys for data-plane verifiers."""
    return get_jwks()
