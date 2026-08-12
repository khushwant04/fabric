"""FastAPI application entrypoint.

Security note: this service is a network-exposed API. Every route except the
health probes, JWKS document, token exchange, and stamp enrollment requires a
verified Fabric credential, and enrollment itself requires a one-time token.
Deploy it behind TLS termination and never expose it without a reverse proxy in
production.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.database import dispose_engine
from app.core.errors import ApiError, api_error_handler

logger = logging.getLogger("fabric.control_plane")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    logger.info("control-plane starting env=%s", settings.app_env)
    try:
        yield
    finally:
        await dispose_engine()
        logger.info("control-plane stopped")


def create_app() -> FastAPI:
    """Build the ASGI application."""
    app = FastAPI(
        title="Fabric Control Plane",
        version="0.1.0",
        summary="Accounts, credentials, deployment intent, and inference stamps.",
        lifespan=lifespan,
    )
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(api_router)
    return app


app = create_app()
