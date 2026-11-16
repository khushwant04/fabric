"""``python -m fabric_data_plane.edge``: serve the edge's public and admin listeners."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import uvicorn

from fabric_data_plane.edge.app import EdgeGateway, create_edge_admin_app, create_edge_app
from fabric_data_plane.edge.config import EdgeSettings
from fabric_data_plane.edge.poller import refresh_keys

logger = logging.getLogger("fabric_data_plane.edge")


async def serve(settings: EdgeSettings | None = None) -> None:
    resolved = settings or EdgeSettings()
    edge = EdgeGateway(resolved)
    public = uvicorn.Server(
        uvicorn.Config(
            create_edge_app(edge),
            host=resolved.host,
            port=resolved.port,
            log_level=resolved.log_level.lower(),
            # Behind an ingress, so client addresses come from forwarding headers.
            proxy_headers=True,
            forwarded_allow_ips="*",
            # Uvicorn's own access log prints paths and client addresses, never bodies or
            # headers, so tokens cannot reach it.
            timeout_keep_alive=75,
        )
    )
    admin = uvicorn.Server(
        uvicorn.Config(
            create_edge_admin_app(edge),
            host=resolved.admin_host,
            port=resolved.admin_port,
            log_level=resolved.log_level.lower(),
            access_log=False,
        )
    )
    background = [
        asyncio.create_task(edge.poller.run(), name="routing-table-poller"),
        asyncio.create_task(refresh_keys(edge.keys, resolved), name="key-refresher"),
    ]
    logger.info(
        "edge serving on %s:%s, admin on %s:%s, control plane %s, table cache %s",
        resolved.host,
        resolved.port,
        resolved.admin_host,
        resolved.admin_port,
        resolved.control_plane_url,
        resolved.table_cache_path or "(memory only)",
    )
    try:
        async with asyncio.TaskGroup() as group:
            tasks = {group.create_task(public.serve()), group.create_task(admin.serve())}
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception() is not None:
                    raise task.exception()  # type: ignore[misc]
            public.should_exit = True
            admin.should_exit = True
    finally:
        for task in background:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await edge.stamp_client.aclose()
        await edge.control_client.aclose()


def main() -> None:
    settings = EdgeSettings()
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
