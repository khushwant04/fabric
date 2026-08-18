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
import math
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, Header, Request, Response, status
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
    TooManyRequests,
    UpstreamUnavailable,
    api_error_handler,
)
from fabric_data_plane.limits import ConcurrencyLimiter, RateLimit, RateLimiter
from fabric_data_plane.metrics import Metrics
from fabric_data_plane.registry import Deployment, ReloadingRegistry
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
        # Either a DeploymentRegistry or a ReloadingRegistry: the data plane only
        # reads, so anything with resolve/for_account/__len__ serves.
        registry: Any,
        client: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.keys = keys
        self.registry = registry
        self.client = client
        self.usage = UsageBuffer(settings.usage_buffer_size)
        self.metrics = Metrics()
        self.rate_limiter = RateLimiter(
            RateLimit(
                requests_per_minute=settings.rate_limit_requests_per_minute,
                # A burst of zero with a rate set would refuse everything, so it falls
                # back to a quarter minute's allowance rather than deadlocking.
                burst=settings.rate_limit_burst
                or max(1, settings.rate_limit_requests_per_minute // 4),
            )
        )
        self.concurrency = ConcurrencyLimiter(settings.max_in_flight_per_account)

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
    started = time.perf_counter()
    principal = plane.authenticate(request.headers.get("authorization"))
    payload = await _read_json(request)
    model = _requested_model(payload)

    # Ownership comes from the verified token plus local configuration; the model
    # name only selects a candidate.
    deployment = plane.registry.resolve(model, account_id=principal.account_id)

    # Checked after authorization so an unauthenticated or unauthorized caller cannot
    # consume another account's allowance, and before proxying so a refused request
    # never reaches the GPU.
    wait = plane.rate_limiter.check(principal.account_id)
    if wait is not None:
        # Counted here because a refused request reaches no model host, so it appears in
        # no engine metric: from the engine's side this traffic never happened.
        plane.metrics.refused(
            deployment=str(deployment.deployment_id),
            account=str(principal.account_id),
            reason="rate_limited",
        )
        raise TooManyRequests(
            "rate_limited",
            "This account has exceeded its request rate on this stamp",
            retry_after=max(1, math.ceil(wait)),
        )

    if not await plane.concurrency.acquire(principal.account_id):
        plane.metrics.refused(
            deployment=str(deployment.deployment_id),
            account=str(principal.account_id),
            reason="too_many_in_flight",
        )
        raise TooManyRequests(
            "too_many_in_flight",
            "This account has too many requests in flight on this stamp",
            # A slot frees when a request finishes rather than on a clock, so a second
            # is the honest minimum rather than a computed estimate.
            retry_after=1,
        )

    streaming = bool(payload.get("stream"))
    upstream_payload = {**payload, "model": deployment.upstream_model_name}
    url = f"{deployment.upstream_url}{upstream_path}"
    headers = forwardable_headers(dict(request.headers))

    deployment_label = str(deployment.deployment_id)
    account_label = str(principal.account_id)
    plane.metrics.request_started(deployment_label)

    if not streaming:
        # try/finally rather than a context manager: the slot must be returned whether
        # the upstream answers, fails, or the request is cancelled, and a leaked slot
        # permanently reduces the account's concurrency until the process restarts.
        try:
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
                body = {
                    "error": {"code": "upstream_invalid_response", "message": "Non-JSON reply"}
                }

            usage = body.get("usage") if isinstance(body, dict) else None
            if response.is_success and isinstance(body, dict):
                plane.record_usage(principal, deployment, body, streamed=False)
                # Answer with the customer-facing alias, not the upstream's name.
                body = {**body, "model": deployment.model_alias}
            plane.metrics.request_finished(
                deployment=deployment_label,
                account=account_label,
                outcome="ok" if response.is_success else "upstream_error",
                duration_seconds=time.perf_counter() - started,
                input_tokens=int((usage or {}).get("prompt_tokens") or 0),
                output_tokens=int((usage or {}).get("completion_tokens") or 0),
            )
            return JSONResponse(status_code=response.status_code, content=body)
        except UpstreamUnavailable:
            plane.metrics.request_finished(
                deployment=deployment_label,
                account=account_label,
                outcome="upstream_unavailable",
                duration_seconds=time.perf_counter() - started,
            )
            raise
        finally:
            await plane.concurrency.release(principal.account_id)

    async def stream() -> AsyncIterator[bytes]:
        # A streamed request holds its slot until the last chunk, which is correct: the
        # model is still decoding for it. Releasing at return would let an account open
        # unlimited long-lived streams.
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
        finally:
            # Recorded when the stream ends rather than when it starts, because that is
            # when the model stopped working for this request.
            plane.metrics.request_finished(
                deployment=deployment_label,
                account=account_label,
                outcome="ok_streamed",
                duration_seconds=time.perf_counter() - started,
            )
            await plane.concurrency.release(principal.account_id)

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

    @router.get("/metrics", summary="Prometheus metrics")
    async def metrics() -> Response:
        """Unauthenticated on purpose.

        Prometheus scrapes without credentials, and this exposes counts and latencies
        rather than prompts or replies. It is on the inference listener because that is
        the listener with a Service in front of it; the administrative listener is
        deliberately reachable only from inside the pod.
        """
        return Response(content=plane.metrics.render(), media_type="text/plain; version=0.0.4")

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
    async def readyz(response: Response) -> dict[str, Any]:
        # Readiness requires usable key material; without it no token can be
        # verified, so the listener must not receive traffic. The status code has to
        # say so: a probe reads the code, not the body, so returning 200 with
        # "unavailable" would send traffic to a data plane that rejects everything.
        keys = plane.keys.snapshot()
        ready = keys["keys_held"] > 0
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ready" if ready else "unavailable",
            "keys_held": keys["keys_held"],
            "deployments": len(plane.registry),
        }

    @router.get("/admin/keys", summary="Verification key cache state")
    async def key_state() -> dict[str, Any]:
        return plane.keys.snapshot()

    @router.get("/admin/upstream", summary="How the model host is reached")
    async def upstream_state() -> dict[str, Any]:
        settings = plane.settings
        return {
            # Reported rather than inferred from a successful request, so a stamp that
            # believes it uses mTLS and does not can be told apart from one that does.
            "client_certificate_configured": bool(settings.upstream_client_cert),
            "authority_pinned": bool(settings.upstream_ca_bundle),
            "mutual_tls": bool(settings.upstream_client_cert and settings.upstream_ca_bundle),
        }

    @router.get("/admin/limits", summary="Rate limit and concurrency state")
    async def limit_state() -> dict[str, Any]:
        return {
            "rate_limit": plane.rate_limiter.snapshot(),
            "concurrency": plane.concurrency.snapshot(),
        }

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


def upstream_tls(settings: Settings) -> dict[str, Any]:
    """Client options for reaching the model host.

    Returns nothing to configure when no material is set, so the default stays plain
    HTTP rather than half-configured TLS. A partial configuration is refused instead of
    silently downgraded: a client certificate with no key cannot authenticate, and
    discovering that as a connection error at request time is worse than at startup.
    """
    cert = settings.upstream_client_cert
    key = settings.upstream_client_key
    authority = settings.upstream_ca_bundle

    if cert and not key:
        raise ValueError("upstream_client_cert is set without upstream_client_key")
    if key and not cert:
        raise ValueError("upstream_client_key is set without upstream_client_cert")

    options: dict[str, Any] = {}
    if authority:
        # Verification against a named authority rather than the system store: a stamp's
        # model host usually presents a certificate from the cluster's own CA, which the
        # system store does not know.
        options["verify"] = authority
    if cert and key:
        options["cert"] = (cert, key)
    return options


def build_plane(settings: Settings | None = None, keys: Any = None) -> DataPlane:
    from fabric_data_plane.keys import KeyCache

    resolved = settings or get_settings()
    tls = upstream_tls(resolved)
    if tls:
        logger.info(
            "model host connections use TLS (client certificate: %s, pinned authority: %s)",
            "yes" if "cert" in tls else "no",
            "yes" if "verify" in tls else "no",
        )
    return DataPlane(
        settings=resolved,
        keys=keys or KeyCache(resolved),
        # Follows the file the agent rewrites rather than reading it once.
        registry=ReloadingRegistry(resolved.deployments_file),
        client=httpx.AsyncClient(**tls),
    )


def _probe_router(plane: DataPlane) -> APIRouter:
    """Health probes on the inference listener.

    These exist here as well as on the administrative listener because the
    administrative listener binds to localhost only, so a Kubernetes kubelet
    probing the pod's address cannot reach it. Binding that listener wider to make
    probes work would expose a destructive usage drain to the whole cluster, so the
    probes come to the public listener instead.

    They are unauthenticated, which is what a probe requires, and therefore report
    only liveness and readiness. Key and deployment counts stay on the
    administrative listener.
    """
    router = APIRouter(tags=["health"])

    @router.get("/healthz", summary="Liveness probe")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz", summary="Readiness probe")
    async def readyz(response: Response) -> dict[str, str]:
        # Without verification keys every request would be rejected, so the pod
        # must not be sent traffic.
        if plane.keys.snapshot()["keys_held"] == 0:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unavailable"}
        return {"status": "ready"}

    return router


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
    app.include_router(_probe_router(resolved))
    return app


def create_admin_app(plane: DataPlane | None = None) -> FastAPI:
    """Internal listener. Must not be exposed alongside inference (AR-DP03)."""
    resolved = plane or build_plane()
    app = FastAPI(title="Fabric Data Plane Admin", version="0.1.0")
    app.state.plane = resolved
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(build_admin_router(resolved))
    return app
