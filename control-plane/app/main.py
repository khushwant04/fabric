"""FastAPI application entrypoint.

Security note: this service is a network-exposed API. Every route except the
health probes, JWKS document, token exchange, and stamp enrollment requires a
verified Fabric credential, and enrollment itself requires a one-time token.
Deploy it behind TLS termination and never expose it without a reverse proxy in
production.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.database import dispose_engine, get_session_factory
from app.core.errors import ApiError, api_error_handler
from app.core.tenancy import system_context, verify_policies_are_enforceable
from app.services.outbox import OutboxWorker

logger = logging.getLogger("fabric.control_plane")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    logger.info("control-plane starting env=%s", settings.app_env)

    # Policies are enabled and forced by migration, but a superuser or a BYPASSRLS
    # role ignores them while the catalog still reports them as active. That failure
    # is completely silent, so it is checked once here. Advisory rather than fatal:
    # refusing to start would turn a hardening gap into an outage, and a database
    # that is briefly unreachable is not evidence of a misconfiguration.
    try:
        factory = get_session_factory()
        async with factory() as session:
            with system_context():
                await verify_policies_are_enforceable(session)
    except Exception:  # noqa: BLE001 - startup must survive a database hiccup
        logger.warning("could not verify row-level security enforcement", exc_info=True)

    # Delivery runs in the API process for now. A separate deployment would be tidier,
    # but the queue is small, the work is I/O bound, and one fewer moving part is worth
    # more than that tidiness until a real consumer exists. It is cancelled on shutdown
    # rather than left to be killed, so an in-flight batch finishes its transaction.
    worker = OutboxWorker(get_session_factory())
    stopping = False
    delivery = asyncio.create_task(
        worker.run(interval=settings.outbox_interval_seconds, stop=lambda: stopping)
    )

    try:
        yield
    finally:
        stopping = True
        delivery.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await delivery
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
