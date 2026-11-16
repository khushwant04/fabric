"""Run the real edge gateway (ADR 0021) for the console demo, over HTTPS.

`python -m fabric_data_plane.edge` serves plain HTTP behind an ingress that terminates TLS.
The demo has no ingress and the console's production build only accepts an HTTPS gateway,
so this starts the same EdgeGateway, poller and key refresher with the public listener on
the demo's throwaway certificate. Everything else is configured by the edge's own
FABRIC_EDGE_* environment, including stamp_address_overrides, which sends each stamp's
reported https://<stamp>.gw.northstar-labs.example to its local data plane while still
verifying the certificate for that name.

Environment (besides FABRIC_EDGE_*):
  DEMO_EDGE_CERT, DEMO_EDGE_KEY   the public listener's certificate and key
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import uvicorn
from fabric_data_plane.edge.app import EdgeGateway, create_edge_admin_app, create_edge_app
from fabric_data_plane.edge.config import EdgeSettings
from fabric_data_plane.edge.poller import refresh_keys


async def main() -> None:
    settings = EdgeSettings()
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    edge = EdgeGateway(settings)
    public = uvicorn.Server(uvicorn.Config(
        create_edge_app(edge), host=settings.host, port=settings.port, log_level="warning",
        ssl_certfile=os.environ["DEMO_EDGE_CERT"], ssl_keyfile=os.environ["DEMO_EDGE_KEY"],
    ))
    admin = uvicorn.Server(uvicorn.Config(
        create_edge_admin_app(edge), host=settings.admin_host, port=settings.admin_port,
        log_level="warning", access_log=False,
    ))
    background = [
        asyncio.create_task(edge.poller.run(), name="routing-table-poller"),
        asyncio.create_task(refresh_keys(edge.keys, settings), name="key-refresher"),
    ]
    try:
        await asyncio.gather(public.serve(), admin.serve())
    finally:
        for task in background:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await edge.stamp_client.aclose()
        await edge.control_client.aclose()


asyncio.run(main())
