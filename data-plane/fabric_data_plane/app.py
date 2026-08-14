"""Inference ingress.

Two applications are built, because AR-DP03 requires the control and
administrative listeners to be isolated from the inference listener. They are
served on different ports and share no routes:

* ``create_inference_app()`` — OpenAI-compatible, authenticated, public.
* ``create_admin_app()``     — health, readiness, key and usage state, internal.

A request on the inference path performs no control-plane call: keys come from
the local cache and deployments from local configuration (AR-DP02, AR-ID04).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from fabric_data_plane.auth import (
    InferencePrincipal,
    extract_bearer_token,
    forwardable_headers,
    verify_inference_token,
)
from fabric_data_plane.config import Settings, get_settings
from fabric_data_plane.errors import (
    ApiError,
    BadRequest,
    UpstreamUnavailable,
    api_error_handler,
)
from fabric_data_plane.registry import Deployment, DeploymentRegistry
from fabric_data_plane.usage import UsageBuffer, UsageRecord

logger = logging.getLogger("fabric.data_plane")

#: Upstream paths this ingress proxies, keyed by the route it exposes.
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
COMPLETIONS_PATH = "/v1/completions"


class DataPlane:
    """Holds the per-process state an inference request needs."""

    def __init__(
        self,
        settings: Settings,
        keys: Any,
        registry: DeploymentRegistry,
        client: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.keys = keys
        self.registry = registry
        self.client = client
        self.usage = UsageBuffer(settings.usage_buffer_size)

    def authenticate(self, authorization: str | None) -> InferencePrincipal:
        token = extract_bearer_token(authorization)
        return verify_inference_token(token, settings=self.settings, keys=self.keys)

    def record_usage(
        self, principal: InferencePrincipal, deployment: Deployment, payload: Any, streamed: bool
    ) -> None:
        usage = payload.get("usage") if isinstance(payload, dict) else None
        self.usage.record(
            UsageRecord(
                account_id=principal.account_id,
                deployment_id=deployment.deployment_id,
                input_tokens=int((usage or {}).get("prompt_tokens", 0) or 0),
                output_tokens=int((usage or {}).get("completion_tokens", 0) or 0),
                streamed=streamed,
                occurred_at=dt.datetime.now(tz=dt.UTC),
            )
        )


async def _read_json(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise BadRequest("invalid_json", "Request body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise BadRequest("invalid_body", "Request body must be a JSON object")
    return payload


def _requested_model(payload: dict[str, Any]) -> str:
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise BadRequest("model_required", "A model must be specified")
    return model.strip()


async def _proxy(
    plane: DataPlane,
    request: Request,
    upstream_path: str,
) -> JSONResponse | StreamingResponse:
    """Authenticate, authorize, then forward to the deployment's model host."""
    principal = plane.authenticate(request.headers.get("authorization"))
    payload = await _read_json(request)
    model = _requested_model(payload)

    # Ownership comes from the verified token plus local configuration; the model
    # name only selects a candidate.
    deployment = plane.registry.resolve(model, account_id=principal.account_id)

    streaming = bool(payload.get("stream"))
    upstream_payload = {**payload, "model": deployment.upstream_model_name}
    url = f"{deployment.upstream_url}{upstream_path}"
    headers = forwardable_headers(dict(request.headers))

    if not streaming:
        try:
            response = await plane.client.post(
                url,
                json=upstream_payload,
                headers=headers,
                timeout=plane.settings.upstream_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(
                "upstream_unavailable", "The model host did not respond"
            ) from exc

        try:
            body = response.json()
        except ValueError:
            body = {"error": {"code": "upstream_invalid_response", "message": "Non-JSON reply"}}

        if response.is_success and isinstance(body, dict):
            plane.record_usage(principal, deployment, body, streamed=False)
            # Answer with the customer-facing alias, not the upstream's name.
            body = {**body, "model": deployment.model_alias}
        return JSONResponse(status_code=response.status_code, content=body)

    async def stream() -> AsyncIterator[bytes]:
        try:
            async with plane.client.stream(
                "POST",
                url,
                json=upstream_payload,
                headers=headers,
                timeout=plane.settings.upstream_timeout_seconds,
            ) as upstream:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
        except httpx.HTTPError as exc:
            logger.warning("upstream stream failed: %s", exc)
            # The response has already begun, so the only honest signal left is
            # an error event in the stream itself.
            yield b'data: {"error":{"code":"upstream_unavailable"}}\n\n'

    # Token counts are not reliably present in a streamed reply, so the record
    # marks the call without inventing usage numbers.
    plane.record_usage(principal, deployment, {}, streamed=True)
    return StreamingResponse(stream(), media_type="text/event-stream")


def build_inference_router(plane: DataPlane) -> APIRouter:
    router = APIRouter(tags=["inference"])

    @router.get("/v1/models", summary="List models this account may invoke")
    async def list_models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        principal = plane.authenticate(authorization)
        return {
            "object": "list",
            "data": [
                {
                    "id": deployment.model_alias,
                    "object": "model",
                    "owned_by": "fabric",
                }
                for deployment in plane.registry.for_account(principal.account_id)
            ],
        }

    @router.post(CHAT_COMPLETIONS_PATH, summary="Chat completion")
    async def chat_completions(request: Request):
        return await _proxy(plane, request, CHAT_COMPLETIONS_PATH)

    @router.post(COMPLETIONS_PATH, summary="Text completion")
    async def completions(request: Request):
        return await _proxy(plane, request, COMPLETIONS_PATH)

    return router


def build_admin_router(plane: DataPlane) -> APIRouter:
    router = APIRouter(tags=["admin"])

    @router.get("/healthz", summary="Liveness probe")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz", summary="Readiness probe")
    async def readyz() -> dict[str, Any]:
        # Readiness requires usable key material; without it no token can be
        # verified, so the listener must not receive traffic.
        keys = plane.keys.snapshot()
        ready = keys["keys_held"] > 0
        return {
            "status": "ready" if ready else "unavailable",
            "keys_held": keys["keys_held"],
            "deployments": len(plane.registry),
        }

    @router.get("/admin/keys", summary="Verification key cache state")
    async def key_state() -> dict[str, Any]:
        return plane.keys.snapshot()

    @router.get("/admin/usage", summary="Local usage buffer state")
    async def usage_state() -> dict[str, Any]:
        return plane.usage.snapshot()

    @router.post("/admin/usage/drain", summary="Remove and return buffered usage")
    async def drain_usage() -> dict[str, Any]:
        """Hand buffered records to a collector.

        Draining is destructive, so it is a POST on the administrative listener
        rather than a GET on the inference listener: only the collector should be
        able to take records, and taking them must not look like a cacheable read.

        The data plane does not export usage itself. It never holds the telemetry
        credential, which belongs to a collector-only Secret, so the credential
        classes stay separate even though the usage data originates here.
        """
        records = plane.usage.drain()
        return {
            "records": [record.as_dict() for record in records],
            "count": len(records),
        }

    return router


def build_plane(settings: Settings | None = None, keys: Any = None) -> DataPlane:
    from fabric_data_plane.keys import KeyCache

    resolved = settings or get_settings()
    return DataPlane(
        settings=resolved,
        keys=keys or KeyCache(resolved),
        registry=DeploymentRegistry.load(resolved.deployments_file),
        client=httpx.AsyncClient(),
    )


def create_inference_app(plane: DataPlane | None = None) -> FastAPI:
    """Public, authenticated, OpenAI-compatible listener."""
    resolved = plane or build_plane()
    app = FastAPI(
        title="Fabric Inference Data Plane",
        version="0.1.0",
        summary="Authenticated OpenAI-compatible inference ingress.",
    )
    app.state.plane = resolved
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(build_inference_router(resolved))
    return app


def create_admin_app(plane: DataPlane | None = None) -> FastAPI:
    """Internal listener. Must not be exposed alongside inference (AR-DP03)."""
    resolved = plane or build_plane()
    app = FastAPI(title="Fabric Data Plane Admin", version="0.1.0")
    app.state.plane = resolved
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(build_admin_router(resolved))
    return app
