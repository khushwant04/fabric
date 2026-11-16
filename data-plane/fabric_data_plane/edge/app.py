"""The edge gateway: one public inference URL in front of every stamp (ADR 0021).

For each request the edge

1. verifies the caller's Fabric inference token locally, with the same code stamps use,
   and refuses it before anything is forwarded;
2. reads ``model`` from a bounded request body;
3. finds the stamps holding that model *for the caller's account* in the routing table;
4. forwards the request, byte for byte, to one of them, streaming the response back.

The stamp gateway verifies the token again and does everything else: model selection for
``auto``/``cascade``, limits, budgets, metering, retries across its own model hosts. The
edge meters nothing and enforces no limits, so usage is never counted twice.

Nothing here contacts the control plane on a request path. The routing table and keys are
refreshed in the background and served from memory, and from disk after a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from fabric_data_plane import anthropic_messages
from fabric_data_plane.auth import (
    InferencePrincipal,
    extract_bearer_token,
    verify_inference_token,
)
from fabric_data_plane.edge import files as file_routing
from fabric_data_plane.edge.config import EdgeSettings
from fabric_data_plane.edge.metrics import EdgeMetrics
from fabric_data_plane.edge.poller import (
    ControlPlaneClient,
    EdgeKeyCache,
    TablePoller,
)
from fabric_data_plane.edge.routing import StampHealth, order_candidates, rendezvous_order
from fabric_data_plane.edge.table import Placement, TableStore
from fabric_data_plane.errors import (
    ApiError,
    BadRequest,
    NotFound,
    PayloadTooLarge,
    UpstreamUnavailable,
    api_error_handler,
)
from fabric_data_plane.verification import VerificationPolicy

logger = logging.getLogger("fabric_data_plane.edge")

#: Nesting limit for request bodies, the same bound the stamp gateway enforces.
MAX_REQUEST_JSON_DEPTH = 128

RUN_ID_HEADER = "x-fabric-run-id"
REGION_HEADER = "x-fabric-region"
EDGE_STAMP_HEADER = "X-Fabric-Edge-Stamp"
SESSION_HEADERS = ("x-fabric-session", "x-session-id", "x-conversation-id")

#: Connection-scoped headers (RFC 9110 section 7.6.1) never forwarded in either direction.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

JSON_ROUTES = (
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/responses",
    "/v1/agent/runs",
)
MESSAGES_ROUTES = (anthropic_messages.MESSAGES_PATH, anthropic_messages.COUNT_TOKENS_PATH)


def json_depth_exceeds(value: Any, limit: int) -> bool:
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def safe_to_retry(exc: httpx.HTTPError) -> bool:
    """Whether the stamp is known not to have received the request.

    The same rule as the stamp gateway's ``_safe_to_retry_transport_failure``: a POST that
    may have reached a stamp is never replayed, because it could run and bill twice. Only
    a failure to establish the connection is retried on another stamp.
    """
    return isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)


class EdgeGateway:
    """Shared state for the public and admin listeners."""

    def __init__(
        self,
        settings: EdgeSettings,
        *,
        keys: Any = None,
        stamp_client: httpx.AsyncClient | None = None,
        control_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        gateway_settings = settings.gateway_settings()
        self.gateway_settings = gateway_settings
        self.policy = VerificationPolicy(gateway_settings)
        self.keys = keys or EdgeKeyCache(
            gateway_settings,
            policy=self.policy,
            min_refetch_seconds=settings.jwks_min_refetch_seconds,
            client=httpx.Client(
                timeout=settings.jwks_timeout_seconds,
                verify=settings.control_plane_ca_bundle or True,
            ),
        )
        self.store = TableStore(settings.table_cache_path)
        self.store.load_from_disk()
        self.health = StampHealth(
            failure_threshold=settings.stamp_failure_threshold,
            recovery_seconds=settings.stamp_recovery_seconds,
        )
        self.metrics = EdgeMetrics()
        self.stamp_client = stamp_client or httpx.AsyncClient(
            verify=settings.stamp_ca_bundle or True,
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_seconds,
                read=settings.read_timeout_seconds,
                write=settings.write_timeout_seconds,
                pool=settings.connect_timeout_seconds,
            ),
            limits=httpx.Limits(
                max_connections=settings.max_connections,
                max_keepalive_connections=min(200, settings.max_connections),
            ),
            # Redirects from a stamp are the caller's business, never followed here with
            # the caller's credential attached.
            follow_redirects=False,
            trust_env=False,
        )
        self.control_client = control_client or httpx.AsyncClient(
            verify=settings.control_plane_ca_bundle or True
        )
        self.poller = TablePoller(
            settings,
            self.store,
            ControlPlaneClient(settings, self.control_client),
            self.metrics,
        )
        self._overrides = {
            key.lower(): value for key, value in settings.stamp_address_overrides.items()
        }

    # -- authentication -------------------------------------------------------

    def authenticate(self, authorization: str | None) -> InferencePrincipal:
        token = extract_bearer_token(authorization)
        return verify_inference_token(
            token,
            settings=self.gateway_settings,
            keys=self.keys,
            issuer=self.policy.jwt_issuer,
        )

    async def authenticate_async(self, authorization: str | None) -> InferencePrincipal:
        # A key fetch for an unknown key id is blocking I/O; keep it off the event loop.
        return await asyncio.to_thread(self.authenticate, authorization)

    # -- readiness ------------------------------------------------------------

    def readiness(self) -> dict[str, Any]:
        table = self.store.table
        keys_held = self.keys.snapshot()["keys_held"]
        return {
            "ready": table is not None and keys_held > 0,
            "routing_table": table is not None,
            "routing_table_source": self.store.source,
            "keys_held": keys_held,
        }

    def stamp_name(self, stamp_id: str) -> str:
        table = self.store.table
        stamp = table.stamps.get(stamp_id) if table else None
        return stamp.name if stamp else stamp_id

    # -- upstream addressing --------------------------------------------------

    def upstream(self, origin: str, path: str, query: str) -> tuple[str, dict[str, str], dict]:
        """URL to send to, extra headers, and request extensions for one stamp."""
        url = f"{origin}{path}" + (f"?{query}" if query else "")
        parts = urlsplit(origin)
        port = parts.port or 443
        override = self._overrides.get(f"{parts.hostname}:{port}".lower())
        if not override:
            return url, {}, {}
        # Connect elsewhere, but verify the certificate for and address the request to the
        # name the stamp reported, so TLS still proves which stamp answered.
        target = f"https://{override}{path}" + (f"?{query}" if query else "")
        return target, {"host": parts.netloc}, {"sni_hostname": parts.hostname}


# -- request plumbing -------------------------------------------------------------


async def read_bounded(request: Request, maximum: int, label: str) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > maximum:
        raise PayloadTooLarge("request_too_large", f"{label} request exceeds the body limit")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise PayloadTooLarge("request_too_large", f"{label} request exceeds the body limit")
        body.extend(chunk)
    return bytes(body)


def parse_json_model(body: bytes) -> tuple[str | None, dict[str, Any]]:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise BadRequest("invalid_json", "Request body is not valid JSON") from exc
    if json_depth_exceeds(payload, MAX_REQUEST_JSON_DEPTH):
        raise BadRequest("invalid_json", "Request body nests too deeply")
    if not isinstance(payload, dict):
        raise BadRequest("invalid_body", "Request body must be a JSON object")
    model = payload.get("model")
    return (model.strip() if isinstance(model, str) and model.strip() else None), payload


async def multipart_model(request: Request, body: bytes) -> str | None:
    """``model`` from a multipart form whose bytes have already been read and bounded."""
    request._body = body  # Starlette replays a cached body to the form parser.
    try:
        form = await request.form()
    except Exception as exc:  # noqa: BLE001 - any parser failure is the caller's body
        raise BadRequest("invalid_body", "Request body is not a valid multipart form") from exc
    try:
        model = form.get("model")
        return model.strip() if isinstance(model, str) and model.strip() else None
    finally:
        await form.close()


def forward_headers(request: Request, extra: dict[str, str]) -> dict[str, str]:
    """The caller's headers minus hop-by-hop ones, with forwarding headers appended.

    ``Authorization`` and ``x-api-key`` pass through unchanged: the stamp verifies the
    caller's own token, and the edge holds no credential that could stand in for it.
    """
    connection_named = {
        name.strip().lower()
        for name in request.headers.get("connection", "").split(",")
        if name.strip()
    }
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        lowered = name.lower()
        if lowered in HOP_BY_HOP or lowered in connection_named:
            continue
        if lowered in ("host", "content-length", "x-forwarded-for", "x-forwarded-proto",
                       "x-forwarded-host"):
            continue
        headers[lowered] = value
    client_ip = request.client.host if request.client else ""
    prior = request.headers.get("x-forwarded-for")
    headers["x-forwarded-for"] = f"{prior}, {client_ip}" if prior and client_ip else (
        prior or client_ip
    )
    headers["x-forwarded-proto"] = request.url.scheme
    if request.headers.get("host"):
        headers["x-forwarded-host"] = request.headers["host"]
    headers.update(extra)
    return headers


def response_headers(upstream: httpx.Response, stamp_name: str) -> dict[str, str]:
    connection_named = {
        name.strip().lower()
        for name in upstream.headers.get("connection", "").split(",")
        if name.strip()
    }
    headers = {
        name: value
        for name, value in upstream.headers.multi_items()
        if name.lower() not in HOP_BY_HOP and name.lower() not in connection_named
    }
    headers[EDGE_STAMP_HEADER] = stamp_name
    return headers


def affinity(request: Request, payload: dict[str, Any] | None) -> tuple[str | None, bool]:
    """The affinity key and whether it is strict (a run id) or soft (a session)."""
    run_id = request.headers.get(RUN_ID_HEADER, "").strip()
    if run_id:
        return f"run:{run_id}", True
    for header in SESSION_HEADERS:
        value = request.headers.get(header, "").strip()
        if value:
            return f"session:{value}", False
    user = (payload or {}).get("user")
    if isinstance(user, str) and user.strip():
        return f"session:{user.strip()}", False
    return None, False


async def forward(
    edge: EdgeGateway,
    request: Request,
    *,
    principal: InferencePrincipal,
    candidates: list[Placement],
    body: bytes | None,
    affinity_key: str | None = None,
    strict_affinity: bool = False,
    started: float,
) -> Response:
    """Send the request to the first reachable candidate and stream its answer back."""
    region = request.headers.get(REGION_HEADER, "").strip() or edge.settings.default_region
    keyed = f"{principal.account_id}:{affinity_key}" if affinity_key else None
    ordered = order_candidates(
        candidates,
        edge.health,
        region=region,
        affinity_key=keyed,
        strict_affinity=strict_affinity,
    )[: edge.settings.max_attempts]
    if not ordered:
        edge.metrics.request(stamp="none", outcome="no_route")
        raise NotFound("model_not_found", "No such model is available to this account")

    last_error: str | None = None
    for attempt, placement in enumerate(ordered):
        stamp = placement.stamp
        url, extra_headers, extensions = edge.upstream(
            stamp.origin, request.url.path, request.url.query
        )
        outgoing = edge.stamp_client.build_request(
            request.method,
            url,
            headers=forward_headers(request, extra_headers),
            content=body,
            extensions=extensions or None,
        )
        edge.health.acquire(stamp.stamp_id)
        try:
            upstream = await edge.stamp_client.send(outgoing, stream=True)
        except httpx.HTTPError as exc:
            edge.health.release(stamp.stamp_id)
            last_error = type(exc).__name__
            if safe_to_retry(exc):
                if edge.health.record_failure(stamp.stamp_id):
                    edge.metrics.ejected(stamp.name)
                    logger.warning("ejected stamp %s after connection failures", stamp.name)
                edge.metrics.request(stamp=stamp.name, outcome="connect_error")
                if attempt + 1 < len(ordered):
                    edge.metrics.retried(stamp.name)
                continue
            # The stamp may have received the request: never replayed elsewhere.
            edge.metrics.request(stamp=stamp.name, outcome="upstream_error")
            raise UpstreamUnavailable(
                "stamp_unavailable",
                "The stamp did not complete the request",
                stamp=stamp.name,
            ) from exc

        edge.health.record_success(stamp.stamp_id)
        return _stream_back(edge, upstream, stamp_id=stamp.stamp_id, stamp_name=stamp.name,
                            started=started)

    raise UpstreamUnavailable(
        "no_stamp_reachable",
        "No stamp serving this model could be reached",
        attempts=len(ordered),
        last_error=last_error,
    )


def _stream_back(
    edge: EdgeGateway,
    upstream: httpx.Response,
    *,
    stamp_id: str,
    stamp_name: str,
    started: float,
) -> StreamingResponse:
    outcome_class = f"{upstream.status_code // 100}xx"

    async def body() -> AsyncIterator[bytes]:
        outcome = outcome_class
        try:
            # Raw bytes, so compressed or event-stream bodies pass through untouched and
            # each chunk is written as soon as it arrives: nothing is buffered here.
            if upstream.is_stream_consumed:
                # Already in memory (an in-process transport); nothing left to stream.
                if upstream.content:
                    yield upstream.content
            else:
                async for chunk in upstream.aiter_raw():
                    yield chunk
        except httpx.HTTPError:
            outcome = "stream_error"
            # Headers are already sent; ending the stream is the only signal left.
        finally:
            with contextlib.suppress(Exception):
                await upstream.aclose()
            edge.health.release(stamp_id)
            edge.metrics.request(
                stamp=stamp_name, outcome=outcome, seconds=time.monotonic() - started
            )

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=response_headers(upstream, stamp_name),
    )


# -- handlers ---------------------------------------------------------------------


def _messages_error(exc: ApiError) -> JSONResponse:
    return anthropic_messages.error_response(
        exc.status_code, exc.message, retry_after=getattr(exc, "retry_after", None)
    )


def _fail(edge: EdgeGateway, exc: ApiError, *, anthropic: bool) -> Response:
    if exc.status_code in (401, 403):
        edge.metrics.request(stamp="none", outcome="unauthorized")
    elif exc.status_code in (400, 413):
        edge.metrics.request(stamp="none", outcome="bad_request")
    return _messages_error(exc) if anthropic else exc.to_response()


async def handle_model_request(
    edge: EdgeGateway, request: Request, *, kind: str
) -> Response:
    started = time.monotonic()
    anthropic = kind == "messages"
    try:
        authorization = request.headers.get("authorization")
        if anthropic:
            authorization = anthropic_messages.messages_authorization(
                authorization, request.headers.get("x-api-key")
            )
        principal = await edge.authenticate_async(authorization)
        table = edge.store.table
        if table is None:
            raise UpstreamUnavailable("routing_unavailable", "The edge has no routing table yet")

        payload: dict[str, Any] | None = None
        if kind == "audio":
            body = await read_bounded(
                request, edge.settings.max_audio_upload_bytes + 1024 * 1024, "Audio"
            )
            model = await multipart_model(request, body)
        else:
            body = await read_bounded(request, edge.settings.max_json_body_bytes, "JSON")
            model, payload = parse_json_model(body)

        candidates = table.candidates(principal.account_id, model)
        if not candidates:
            edge.metrics.request(stamp="none", outcome="no_route")
            raise NotFound("model_not_found", "No such model is available to this account")
        # file_search reads the account's stores, which live on its file stamp only.
        if request.url.path == "/v1/agent/runs" and file_routing.references_file_search(
            payload
        ):
            home = file_routing.file_stamp(table, principal.account_id)
            candidates = [
                c for c in candidates if home and c.stamp.stamp_id == home.stamp.stamp_id
            ]
            if not candidates:
                edge.metrics.request(stamp="none", outcome="no_route")
                raise BadRequest(
                    "file_search_stamp_mismatch",
                    "This account's vector stores live on a stamp that does not serve this "
                    "model; use a model deployed on that stamp",
                )
        key, strict = affinity(request, payload)
        return await forward(
            edge,
            request,
            principal=principal,
            candidates=candidates,
            body=body,
            affinity_key=key,
            strict_affinity=strict,
            started=started,
        )
    except ApiError as exc:
        return _fail(edge, exc, anthropic=anthropic)


async def handle_file_request(edge: EdgeGateway, request: Request) -> Response:
    """``/v1/files*`` and ``/v1/vector_stores*``: always the account's file stamp."""
    started = time.monotonic()
    try:
        principal = await edge.authenticate_async(request.headers.get("authorization"))
        table = edge.store.table
        if table is None:
            raise UpstreamUnavailable("routing_unavailable", "The edge has no routing table yet")
        home = file_routing.file_stamp(table, principal.account_id)
        if home is None:
            edge.metrics.request(stamp="none", outcome="no_route")
            raise NotFound("file_search_unavailable", "This account has no stamp to hold files")
        body: bytes | None = None
        if request.method == "POST":
            multipart = request.headers.get("content-type", "").startswith("multipart/")
            limit = (
                edge.settings.max_file_upload_bytes + 1024 * 1024
                if multipart
                else edge.settings.max_json_body_bytes
            )
            body = await read_bounded(request, limit, "File" if multipart else "JSON")
        return await forward(
            edge, request, principal=principal, candidates=[home], body=body, started=started
        )
    except ApiError as exc:
        return _fail(edge, exc, anthropic=False)


async def handle_run_lookup(edge: EdgeGateway, request: Request, run_id: str) -> Response:
    """``GET``/``DELETE /v1/runs/{id}``: ask the account's stamps in affinity order.

    A run lives on the stamp its first step went to: the highest-ranked stamp, for this
    account and run id, among those holding the model it used. Rendezvous ranking is
    consistent under restriction, so walking the account's stamps in rank order reaches
    that stamp first in the common case and still finds the run if the model set moved.
    """
    started = time.monotonic()
    try:
        principal = await edge.authenticate_async(request.headers.get("authorization"))
        table = edge.store.table
        if table is None:
            raise UpstreamUnavailable("routing_unavailable", "The edge has no routing table yet")
        stamps = rendezvous_order(
            f"{principal.account_id}:run:{run_id}", table.account_stamps(principal.account_id)
        )
        if not stamps:
            raise NotFound("run_not_found", "No such run for this account")
        last: Response | None = None
        for placement in stamps:
            try:
                response = await forward(
                    edge, request, principal=principal, candidates=[placement], body=None,
                    started=started,
                )
            except UpstreamUnavailable as exc:
                last = exc.to_response()
                continue
            if response.status_code != 404:
                return response
            last = response
        assert last is not None
        return last
    except ApiError as exc:
        return _fail(edge, exc, anthropic=False)


async def handle_run_list(edge: EdgeGateway, request: Request) -> Response:
    """``GET /v1/runs``: runs are stamp-local, so the list is gathered from each stamp."""
    try:
        authorization = request.headers.get("authorization")
        principal = await edge.authenticate_async(authorization)
        table = edge.store.table
        if table is None:
            raise UpstreamUnavailable("routing_unavailable", "The edge has no routing table yet")
        try:
            limit = max(1, min(int(request.query_params.get("limit", "50")), 500))
        except ValueError as exc:
            raise BadRequest("invalid_limit", "limit must be an integer") from exc

        async def one(placement: Placement) -> list[dict[str, Any]]:
            url, extra, extensions = edge.upstream(
                placement.stamp.origin, "/v1/runs", f"limit={limit}"
            )
            try:
                response = await edge.stamp_client.get(
                    url,
                    headers={"authorization": authorization or "", **extra},
                    extensions=extensions or None,
                )
            except httpx.HTTPError:
                return []
            if response.status_code != 200:
                return []
            data = response.json().get("data") or []
            for item in data:
                if isinstance(item, dict):
                    item.setdefault("stamp", placement.stamp.name)
            return [item for item in data if isinstance(item, dict)]

        gathered = await asyncio.gather(
            *(one(placement) for placement in table.account_stamps(principal.account_id))
        )
        runs = [item for chunk in gathered for item in chunk]
        return JSONResponse({"object": "list", "data": runs[:limit]})
    except ApiError as exc:
        return _fail(edge, exc, anthropic=False)


def build_inference_router(edge: EdgeGateway) -> APIRouter:
    router = APIRouter(tags=["edge"])

    @router.get("/v1/models", summary="Models this account may invoke, across all stamps")
    async def list_models(request: Request) -> Response:
        try:
            principal = await edge.authenticate_async(request.headers.get("authorization"))
        except ApiError as exc:
            return _fail(edge, exc, anthropic=False)
        table = edge.store.table
        names = table.model_names(principal.account_id) if table else []
        return JSONResponse(
            {
                "object": "list",
                "data": [{"id": name, "object": "model", "owned_by": "fabric"} for name in names],
            }
        )

    for path in JSON_ROUTES:

        async def json_route(request: Request) -> Response:
            return await handle_model_request(edge, request, kind="json")

        router.add_api_route(path, json_route, methods=["POST"], include_in_schema=True)

    for path in MESSAGES_ROUTES:

        async def messages_route(request: Request) -> Response:
            return await handle_model_request(edge, request, kind="messages")

        router.add_api_route(path, messages_route, methods=["POST"])

    @router.post("/v1/audio/{operation}", summary="Audio transcription and translation")
    async def audio(operation: str, request: Request) -> Response:
        return await handle_model_request(edge, request, kind="audio")

    # File search state is stamp-local: every call of an account goes to its file stamp.
    for prefix in file_routing.FILE_ROUTE_PREFIXES:

        async def file_route(request: Request) -> Response:
            return await handle_file_request(edge, request)

        router.add_api_route(prefix, file_route, methods=["GET", "POST"])
        router.add_api_route(
            prefix + "/{rest:path}", file_route, methods=["GET", "POST", "DELETE"]
        )

    @router.get("/v1/runs", summary="This account's agent runs, gathered from its stamps")
    async def list_runs(request: Request) -> Response:
        return await handle_run_list(edge, request)

    @router.api_route("/v1/runs/{run_id}", methods=["GET", "DELETE"], summary="One agent run")
    async def run(run_id: str, request: Request) -> Response:
        return await handle_run_lookup(edge, request, run_id)

    return router


def build_probe_router(edge: EdgeGateway) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz")
    async def readyz(response: Response) -> dict[str, str]:
        if not edge.readiness()["ready"]:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unavailable"}
        return {"status": "ready"}

    return router


def build_admin_router(edge: EdgeGateway) -> APIRouter:
    router = APIRouter(tags=["admin"])

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz")
    async def readyz(response: Response) -> dict[str, Any]:
        state = edge.readiness()
        if not state["ready"]:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return state

    @router.get("/metrics")
    async def metrics() -> Response:
        table = edge.store.table
        return Response(
            content=edge.metrics.render(
                table_age=edge.store.age_seconds(),
                table_revision=table.revision if table else 0,
                table_from_disk=edge.store.source == "disk",
                keys_held=edge.keys.snapshot()["keys_held"],
                stamp_states=edge.health.snapshot(),
                stamp_names=edge.stamp_name,
            ),
            media_type="text/plain; version=0.0.4",
        )

    @router.get("/admin/routing", summary="Routing table summary and stamp health")
    async def routing() -> dict[str, Any]:
        table = edge.store.table
        health = edge.health.snapshot()
        summary = table.summary() if table else None
        if summary:
            for stamp in summary["stamps"]:
                stamp["health"] = health.get(
                    stamp["stamp_id"],
                    {"in_flight": 0, "consecutive_failures": 0, "ejected": False,
                     "ejected_for_seconds": 0.0, "ejections": 0},
                )
        return {
            "table": summary,
            "source": edge.store.source,
            "age_seconds": edge.store.age_seconds(),
            "last_poll_error": edge.poller.last_error,
            "last_poll_success": edge.poller.last_success,
            "keys_held": edge.keys.snapshot()["keys_held"],
            "metering": "none: stamps meter every request they serve",
        }

    return router


def create_edge_app(edge: EdgeGateway) -> FastAPI:
    """Public listener: inference routes and probes."""
    app = FastAPI(
        title="Fabric Edge Gateway",
        version="0.1.0",
        summary="One inference URL in front of every Fabric stamp.",
    )
    app.state.edge = edge
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(build_inference_router(edge))
    app.include_router(build_probe_router(edge))
    return app


def create_edge_admin_app(edge: EdgeGateway) -> FastAPI:
    app = FastAPI(title="Fabric Edge Admin", version="0.1.0")
    app.state.edge = edge
    app.include_router(build_admin_router(edge))
    return app
