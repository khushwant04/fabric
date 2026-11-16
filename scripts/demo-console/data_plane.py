"""Run one stamp's real data plane for the console demo.

The same arrangement agent/scripts/e2e.sh uses: the inference and admin listeners share one
process (and so one usage buffer, which the collector drains), the deployments come from
the deployments.json the Go agent wrote, and tokens are verified against the control
plane's published signing keys. Unlike e2e.sh the upstream is a real HTTP client, pointed
by the agent's --upstream at the demo model host.

Environment:
  DEMO_DEPLOYMENTS   path to the agent-written deployments.json
  DEMO_JWKS          path to a copy of the control plane's /.well-known/jwks.json
  DEMO_JWT_ISSUER    the control plane's FABRIC_JWT_ISSUER
  DEMO_DP_PORT, DEMO_DP_ADMIN_PORT
  DEMO_DP_CERT, DEMO_DP_KEY   serve the inference listener over HTTPS
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import uvicorn
from fabric_data_plane.app import DataPlane, create_admin_app, create_inference_app
from fabric_data_plane.config import Settings
from fabric_data_plane.keys import KeyCache
from fabric_data_plane.registry import DeploymentRegistry

deployments = os.environ["DEMO_DEPLOYMENTS"]
jwks_path = os.environ["DEMO_JWKS"]
issuer = os.environ["DEMO_JWT_ISSUER"]

settings = Settings(
    app_env="test",
    jwt_issuer=issuer,
    jwks_url=f"{issuer}/.well-known/jwks.json",
    jwks_file=jwks_path,
    deployments_file=deployments,
)

registry = DeploymentRegistry.load(deployments)

plane = DataPlane(
    settings=settings,
    keys=KeyCache(settings, client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=json.load(open(jwks_path)))
    ))),
    registry=registry,
    client=httpx.AsyncClient(timeout=120.0),
)


async def main() -> None:
    inference = uvicorn.Server(uvicorn.Config(
        create_inference_app(plane), host="127.0.0.1",
        port=int(os.environ["DEMO_DP_PORT"]), log_level="warning",
        # HTTPS like a real gateway, with the demo's throwaway CA: the console only accepts an
        # HTTPS inference URL in a production build.
        ssl_certfile=os.environ.get("DEMO_DP_CERT"), ssl_keyfile=os.environ.get("DEMO_DP_KEY"),
    ))
    admin = uvicorn.Server(uvicorn.Config(
        create_admin_app(plane), host="127.0.0.1",
        port=int(os.environ["DEMO_DP_ADMIN_PORT"]), log_level="warning",
    ))
    await asyncio.gather(inference.serve(), admin.serve())


asyncio.run(main())
