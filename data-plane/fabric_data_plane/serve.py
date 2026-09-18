"""Process entrypoint serving the public and private listeners.

Inference, administration, rollout status, and optional shared-limit coordination are separate ASGI
applications on separate ports. They share one DataPlane object: usage accounting and local
lifecycle state have one owner, while only inference is ever publicly exposed.

    python -m fabric_data_plane.serve
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from fabric_data_plane.app import (
    build_plane,
    create_admin_app,
    create_inference_app,
    create_limit_coordinator_app,
    create_router_status_app,
)
from fabric_data_plane.config import Settings

logger = logging.getLogger("fabric_data_plane.serve")


async def serve(settings: Settings | None = None) -> None:
    """Serve inference and administrative listeners until cancelled."""
    resolved = settings or Settings()
    plane = build_plane(resolved)

    inference = uvicorn.Server(
        uvicorn.Config(
            create_inference_app(plane),
            host=resolved.host,
            port=resolved.port,
            # uvicorn maps this through a lookup table keyed by lowercase names.
            log_level=resolved.log_level.lower(),
            # Proxy headers are honoured because the data plane sits behind an
            # ingress; without this, client addresses in logs are the ingress.
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    )
    admin = uvicorn.Server(
        uvicorn.Config(
            create_admin_app(plane),
            # Bound to all interfaces inside the pod's namespace only because the
            # Service does not publish this port. It must never be exposed: it
            # drains usage and reports internal state.
            host=resolved.admin_host,
            port=resolved.admin_port,
            log_level=resolved.log_level.lower(),
        )
    )

    router_status = uvicorn.Server(
        uvicorn.Config(
            create_router_status_app(plane),
            host=resolved.router_status_host,
            port=resolved.router_status_port,
            log_level=resolved.log_level.lower(),
        )
    )
    limit_coordinator = None
    if plane.limit_store is not None:
        limit_coordinator = uvicorn.Server(
            uvicorn.Config(
                create_limit_coordinator_app(plane),
                host=resolved.limit_coordinator_host,
                port=resolved.limit_coordinator_port,
                log_level=resolved.log_level.lower(),
            )
        )

    # Warm the verification keys before readiness is polled, and keep retrying: a
    # readiness check that requires keys can never pass if keys are only fetched
    # while serving a request, because no request arrives until the pod is ready.
    warmer = asyncio.create_task(_warm_keys(plane, resolved))

    logger.info(
        "serving inference on %s:%s, administration on %s:%s, router status on %s:%s%s",
        resolved.host,
        resolved.port,
        resolved.admin_host,
        resolved.admin_port,
        resolved.router_status_host,
        resolved.router_status_port,
        (
            f", and shared limits on {resolved.limit_coordinator_host}:"
            f"{resolved.limit_coordinator_port}"
            if limit_coordinator is not None
            else ""
        ),
    )

    # If either listener stops, the other is torn down: a data plane serving
    # inference with no drainable buffer, or a drainable buffer with no traffic,
    # is a half-failed pod that should be restarted rather than left running.
    async with asyncio.TaskGroup() as group:
        inference_task = group.create_task(inference.serve())
        admin_task = group.create_task(admin.serve())
        router_status_task = group.create_task(router_status.serve())
        tasks = {inference_task, admin_task, router_status_task}
        if limit_coordinator is not None:
            tasks.add(group.create_task(limit_coordinator.serve()))

        done, _pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task.exception() is not None:
                raise task.exception()  # type: ignore[misc]

        inference.should_exit = True
        admin.should_exit = True
        router_status.should_exit = True
        if limit_coordinator is not None:
            limit_coordinator.should_exit = True
        warmer.cancel()


async def _warm_keys(plane, settings: Settings) -> None:
    """Fetch verification keys until some are held, then refresh periodically.

    The fetch is synchronous, so it runs in a worker thread to avoid blocking the
    listeners. A failure is logged and retried: the control plane may simply not be
    reachable yet, and the pod stays unready until it is.
    """
    delay = 2.0
    while True:
        held = plane.keys.snapshot()["keys_held"]
        if held == 0:
            try:
                await asyncio.to_thread(plane.keys.refresh)
            except Exception as error:  # noqa: BLE001 - startup must not crash on this
                logger.warning("could not warm verification keys: %s", error)
            held = plane.keys.snapshot()["keys_held"]
            if held == 0:
                await asyncio.sleep(delay)
                # Backing off to the configured refresh interval keeps a long
                # control-plane outage from hammering it.
                delay = min(delay * 2, float(settings.jwks_refresh_seconds))
                continue
            logger.info("verification keys warmed: %d held", held)

        await asyncio.sleep(settings.jwks_refresh_seconds)
        try:
            await asyncio.to_thread(plane.keys.refresh)
        except Exception as error:  # noqa: BLE001
            logger.warning("key refresh failed, keeping cached keys: %s", error)


def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:  # pragma: no cover - signal path
        logger.info("shutting down")


if __name__ == "__main__":
    main()
