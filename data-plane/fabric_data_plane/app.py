"""Inference ingress.

Separate applications keep public inference isolated from administration, rollout status, and
stamp-local limit coordination. They are served on different ports and share no routes:

* ``create_inference_app()`` — OpenAI-compatible, authenticated, public;
* ``create_admin_app()`` — health, readiness, key and usage state, pod-local;
* ``create_router_status_app()`` — rollout acknowledgement, operator-only;
* ``create_limit_coordinator_app()`` — shared account admission, gateway-only.

A request on the inference path performs no control-plane call: keys come from
the local cache and deployments from local configuration (AR-DP02, AR-ID04).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
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
from fabric_data_plane.config import (
    INFERENCE_AUDIENCE,
    INFERENCE_SCOPE,
    Settings,
    get_settings,
)
from fabric_data_plane.errors import (
    ApiError,
    BadRequest,
    NotFound,
    PayloadTooLarge,
    TooManyRequests,
    UpstreamUnavailable,
    api_error_handler,
)
from fabric_data_plane.limits import ConcurrencyLimiter, RateLimit, RateLimiter
from fabric_data_plane.metrics import Metrics
from fabric_data_plane.model_selection import (
    AUTO_MODEL,
    profile_for_audio,
    profile_for_json,
    rank_deployments,
)
from fabric_data_plane.pool import Backend, BackendPool
from fabric_data_plane.registry import Deployment, ReloadingRegistry
from fabric_data_plane.shared_limits import (
    LimitCoordinatorStore,
    LimitCoordinatorUnavailable,
    LocalLimitManager,
    SharedLimitManager,
)
from fabric_data_plane.shared_limits import (
    authorized as limit_authorized,
)
from fabric_data_plane.streaming import UsageMeter, streaming_upstream_payload
from fabric_data_plane.usage import (
    UsageBuffer,
    UsageLeaseMismatch,
    UsageRecord,
    UsageSpoolUnavailable,
)
from fabric_data_plane.verification import VerificationPolicy

logger = logging.getLogger("fabric.data_plane")

#: Upstream paths this ingress proxies, keyed by the route it exposes.
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
COMPLETIONS_PATH = "/v1/completions"
TRANSCRIPTIONS_PATH = "/v1/audio/transcriptions"
TRANSLATIONS_PATH = "/v1/audio/translations"

_JSON_OPERATIONS = {
    CHAT_COMPLETIONS_PATH: "chat",
    COMPLETIONS_PATH: "completion",
}
_AUDIO_OPERATIONS = {
    TRANSCRIPTIONS_PATH: "transcription",
    TRANSLATIONS_PATH: "translation",
}


class _CleanupStreamingResponse(StreamingResponse):
    """Run cleanup even when response-start fails before iteration begins."""

    def __init__(
        self,
        content: AsyncIterator[bytes],
        *,
        cleanup: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(content, media_type="text/event-stream")
        self._cleanup = cleanup

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._cleanup()


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
        policy: VerificationPolicy | None = None,
    ) -> None:
        self.settings = settings
        self.keys = keys
        self.registry = registry
        self.client = client
        # One policy decides both what issuer a token is checked against and where keys
        # come from, so it is shared with the key cache rather than duplicated: two
        # copies could adopt at different moments and verify tokens with the wrong keys.
        self.verification = (
            policy or getattr(keys, "policy", None) or VerificationPolicy(settings)
        )
        # Issuer and key material form one verification generation. Authentication and
        # desired-state adoption share this lock so a request observes either the old
        # pair or the fully prepared new pair, never half of each.
        self._verification_lock = threading.RLock()
        self.usage = UsageBuffer(
            settings.usage_buffer_size,
            path=settings.usage_spool_path,
        )
        self.metrics = Metrics()
        # One pool per deployment, memoised so per-backend health survives across the
        # requests that deployment serves rather than resetting each call. Keyed by
        # deployment id because the registry may hand back an equal-but-new Deployment on
        # a reload; the health that matters is per stable deployment, not per object.
        self._pools: dict[uuid.UUID, BackendPool] = {}
        # Every draining generation is retained. A stable deployment id can be withdrawn,
        # reintroduced, and withdrawn again before its oldest attempt ends; one slot per id would
        # overwrite that older pool and make retirement forget a live backend lease.
        self._retired_pools: dict[uuid.UUID, list[BackendPool]] = {}
        # Registry identities whose placement-level metric baselines are currently published.
        # Kept separately from pools because an idle deployment has metrics before its first
        # request builds a pool.
        self._known_deployments: set[uuid.UUID] = set()
        # Streams are admitted before Starlette enters their body iterator. Keep that interval
        # visible to placement retirement too: a registry withdrawal must not tombstone the loss
        # label of a captured request that can still reach its host.
        self._admitted_streams: dict[uuid.UUID, int] = {}
        self._pools_lock = threading.Lock()
        rate_limit = RateLimit(
            requests_per_minute=settings.rate_limit_requests_per_minute,
            # A burst of zero with a rate set would refuse everything, so it falls
            # back to a quarter minute's allowance rather than deadlocking.
            burst=settings.rate_limit_burst
            or max(1, settings.rate_limit_requests_per_minute // 4),
        )
        self.rate_limiter = RateLimiter(rate_limit)
        self.concurrency = ConcurrencyLimiter(settings.max_in_flight_per_account)
        self.limit_store = (
            LimitCoordinatorStore(
                path=settings.limit_coordinator_store_path,
                rate=rate_limit,
                maximum=settings.max_in_flight_per_account,
                lease_seconds=settings.limit_lease_seconds,
            )
            if settings.limit_coordinator_store_path
            else None
        )
        if settings.limit_coordinator_url:
            self.limits = SharedLimitManager(
                base_url=settings.limit_coordinator_url,
                token=settings.limit_coordinator_token or "",
                timeout=settings.limit_coordinator_timeout_seconds,
                renew_seconds=settings.limit_renew_seconds,
                rate_enabled=rate_limit.enabled,
                concurrency_enabled=settings.max_in_flight_per_account > 0,
            )
        else:
            self.limits = LocalLimitManager(self.rate_limiter, self.concurrency)
        # Publish zero-valued placement counters before the first request can finish. A counter
        # first observed at one hides that first event from Prometheus rate()/increase().
        self.reconcile_pools()

    def authenticate(self, authorization: str | None) -> InferencePrincipal:
        token = extract_bearer_token(authorization)
        with self._verification_lock:
            return verify_inference_token(
                token,
                settings=self.settings,
                keys=self.keys,
                issuer=self.verification.jwt_issuer,
            )

    def _adopt_verification(self) -> None:
        """Prepare and atomically activate the contract the agent last wrote."""
        reported = getattr(self.registry, "reported_verification", None)
        if not callable(reported):
            return
        candidate = reported()
        with self._verification_lock:
            if candidate is None:
                return
            current = self.verification.effective
            if candidate == current:
                self.verification.apply(candidate)
                return
            if not candidate.complete:
                self.verification.apply(candidate)
                return
            if candidate.jwks_url != current.jwks_url:
                refresh = getattr(self.keys, "refresh_source", None)
                # A custom cache that cannot prepare an explicit source must not cause the
                # issuer to move while retaining keys from the old generation.
                if not callable(refresh) or not refresh(candidate.jwks_url):
                    return
            # New-source keys are installed while authentication is excluded; publish the
            # issuer before releasing the lock, making the transition one generation.
            self.verification.apply(candidate)

    def reconcile_pools(self) -> None:
        """Activate current placements and retire state for placements no longer present."""
        self._adopt_verification()
        verification = self.verification.snapshot()
        self.metrics.verification_state(
            synced=verification["source"] == "synced",
            matches_local=verification["matches_local"],
            rejected_updates=verification["rejected_updates"],
            corrected_drift=verification["corrected_drift"],
        )
        deployments = getattr(self.registry, "deployments", None)
        if callable(deployments):
            # One immutable snapshot supplies both ids and account labels. Separate registry
            # calls can straddle a lazy reload and briefly pair identities from different views.
            current = deployments()
            active = {deployment.deployment_id for deployment in current}
            for deployment in current:
                self.metrics.activate_deployment(
                    deployment=str(deployment.deployment_id),
                    account=str(deployment.account_id),
                )
        else:
            deployment_ids = getattr(self.registry, "deployment_ids", None)
            if not callable(deployment_ids):
                return
            active = deployment_ids()

        with self._pools_lock:
            withdrawn = self._known_deployments - active
            self._known_deployments = set(active)
            for stale_id in self._pools.keys() - active:
                pool = self._pools.pop(stale_id)
                admitted = self._admitted_streams.get(stale_id, 0)
                current_in_flight = sum(pool.health.in_flight().values())
                retired_in_flight = sum(
                    sum(retired.health.in_flight().values())
                    for retired in self._retired_pools.get(stale_id, [])
                )
                if current_in_flight > 0 or admitted > 0:
                    self._retired_pools.setdefault(stale_id, []).append(pool)
                elif retired_in_flight == 0:
                    # An idle current generation may coexist with an older draining generation.
                    # Retirement is stable-id wide, so it must include every generation's lease.
                    self.metrics.retire_deployment(str(stale_id))
            # An idle placement has no pool but still has the zero baselines published above.
            # Remove its active membership when the registry withdraws it; process-lifetime
            # counter values stay published. An admitted stream or active retired pool keeps
            # membership until its last cleanup/drain.
            for stale_id in withdrawn:
                if (
                    stale_id not in self._retired_pools
                    and stale_id not in self._pools
                    and self._admitted_streams.get(stale_id, 0) == 0
                ):
                    self.metrics.retire_deployment(str(stale_id))

    def pool_for(self, deployment: Deployment) -> BackendPool:
        """Return the backend pool for a deployment, built once and reused.

        Memoised by deployment id so per-backend health accumulates across requests. A
        reload that changes a deployment's backend set rebuilds the pool, so a departed
        backend is not carried forward under stale health.
        """
        self.reconcile_pools()
        # Defensive fallback for registry implementations that can resolve a Deployment but do
        # not expose deployments() for proactive activation.
        self.metrics.activate_deployment(
            deployment=str(deployment.deployment_id),
            account=str(deployment.account_id),
        )
        with self._pools_lock:
            self._known_deployments.add(deployment.deployment_id)
            pool = self._pools.get(deployment.deployment_id)
            current_backends = tuple(
                (backend.backend_id, backend.url, backend.weight, backend.workload)
                for backend in deployment.backends
            )
            current_identity = (deployment.strategy, current_backends)
            pool_identity = None
            if pool is not None:
                previous_backends = tuple(
                    (backend.backend_id, backend.url, backend.weight, backend.workload)
                    for backend in pool.backends
                )
                pool_identity = (pool.strategy_name, previous_backends)
            if pool is None or pool_identity != current_identity:
                previous_ids = (
                    {backend.backend_id for backend in pool.backends} if pool else set()
                )
                current_ids = {backend.backend_id for backend in deployment.backends}
                for stale_backend in previous_ids - current_ids:
                    self.metrics.retire_backend(
                        deployment=str(deployment.deployment_id),
                        backend=stale_backend,
                    )

                # Preserve overlapping backend health and active attempts while adding a
                # rollout candidate. This is essential for drain acknowledgement: streams
                # started on the old pool must remain visible after the cutover document
                # adds candidate backend ids.
                if pool is not None:
                    pool.health.reconcile(list(deployment.backends))
                    pool = BackendPool(
                        list(deployment.backends),
                        health=pool.health,
                        strategy=deployment.strategy,
                    )
                else:
                    pool = deployment.build_pool(
                        failure_threshold=self.settings.backend_failure_threshold,
                        recovery_seconds=self.settings.backend_recovery_seconds,
                    )
                self._pools[deployment.deployment_id] = pool

            snapshot = pool.health.snapshot()
            for backend in pool.backends:
                state = snapshot[backend.backend_id]
                self.metrics.activate_backend(
                    deployment=str(deployment.deployment_id),
                    backend=backend.backend_id,
                    available=bool(state["available"]),
                )
            return pool

    def retain_stream(self, deployment: Deployment) -> None:
        """Hold placement metric membership from response admission through cleanup."""
        deployment_id = deployment.deployment_id
        self.metrics.activate_deployment(
            deployment=str(deployment_id),
            account=str(deployment.account_id),
        )
        with self._pools_lock:
            self._admitted_streams[deployment_id] = (
                self._admitted_streams.get(deployment_id, 0) + 1
            )

    def release_stream(self, deployment: Deployment) -> None:
        """Release an admitted stream, tombstoning membership only after its loss was counted."""
        deployment_id = deployment.deployment_id
        retire = False
        with self._pools_lock:
            remaining = self._admitted_streams.get(deployment_id, 0) - 1
            if remaining > 0:
                self._admitted_streams[deployment_id] = remaining
            else:
                self._admitted_streams.pop(deployment_id, None)
                retired_pools = self._retired_pools.get(deployment_id, [])
                backend_in_flight = sum(
                    sum(pool.health.in_flight().values()) for pool in retired_pools
                )
                retire = (
                    deployment_id not in self._known_deployments
                    and backend_in_flight == 0
                )
                if retire and retired_pools:
                    del self._retired_pools[deployment_id]
        if retire:
            # Counter values are process-lifetime history and remain exposed; this only prevents
            # a cleanup that was not retained before withdrawal from creating a new event.
            self.metrics.retire_deployment(str(deployment_id))

    def router_state(self) -> dict[str, Any]:
        """Loaded route revision and active attempts for acknowledged rollout drain."""
        # A removal-to-empty document has no deployments to pass through pool_for, so
        # reconcile first to move any active old pool into retired drain tracking.
        self.reconcile_pools()
        deployments = getattr(self.registry, "deployments", lambda: [])()
        revision = getattr(self.registry, "config_revision", lambda: "")()
        entries: list[dict[str, Any]] = []
        for deployment in deployments:
            pool = self.pool_for(deployment)
            counts = pool.health.in_flight()
            workloads = pool.health.workloads()
            current_ids = set()
            for backend in pool.backends:
                current_ids.add(backend.backend_id)
                entries.append(
                    {
                        "deployment_id": str(deployment.deployment_id),
                        "backend_id": backend.backend_id,
                        "route_revision": deployment.route_revision,
                        "workload": backend.workload,
                        "in_flight": counts.get(backend.backend_id, 0),
                    }
                )
            # BackendHealth retains a removed identity while an old ordinary/streamed
            # attempt still uses it. Expose it even though it is no longer selectable;
            # omission must never be interpreted as zero by rollout drain.
            for backend_id, in_flight in counts.items():
                if backend_id not in current_ids and in_flight > 0:
                    entries.append(
                        {
                            "deployment_id": str(deployment.deployment_id),
                            "backend_id": backend_id,
                            "route_revision": deployment.route_revision,
                            "workload": workloads.get(backend_id, ""),
                            "in_flight": in_flight,
                            "retired": True,
                        }
                    )
        with self._pools_lock:
            for deployment_id, pools in list(self._retired_pools.items()):
                # Drop drained generations individually, never the stable-id slot wholesale:
                # repeated withdraw/reintroduce cycles may leave several generations alive.
                draining = [
                    pool for pool in pools if sum(pool.health.in_flight().values()) > 0
                ]
                admitted = self._admitted_streams.get(deployment_id, 0)
                if not draining and admitted == 0:
                    del self._retired_pools[deployment_id]
                    # The same stable id may have been reintroduced while an old generation
                    # drained. Do not let old-pool retirement erase new placement membership.
                    if deployment_id not in self._known_deployments:
                        self.metrics.retire_deployment(str(deployment_id))
                    continue
                self._retired_pools[deployment_id] = draining or pools

                # Router state has no generation label, so aggregate identical backend ids across
                # retired generations instead of emitting ambiguous duplicate rows.
                retired_counts: dict[str, int] = {}
                retired_workloads: dict[str, str] = {}
                for pool in draining:
                    workloads = pool.health.workloads()
                    for backend_id, in_flight in pool.health.in_flight().items():
                        if in_flight <= 0:
                            continue
                        retired_counts[backend_id] = (
                            retired_counts.get(backend_id, 0) + in_flight
                        )
                        retired_workloads.setdefault(
                            backend_id, workloads.get(backend_id, "")
                        )
                for backend_id, in_flight in retired_counts.items():
                    entries.append(
                        {
                            "deployment_id": str(deployment_id),
                            "backend_id": backend_id,
                            "route_revision": "",
                            "workload": retired_workloads[backend_id],
                            "in_flight": in_flight,
                            "retired": True,
                        }
                    )
        return {"revision": revision, "backends": entries}

    def record_usage(
        self, principal: InferencePrincipal, deployment: Deployment, payload: Any, streamed: bool
    ) -> None:
        usage = payload.get("usage") if isinstance(payload, dict) else None
        try:
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
        except UsageSpoolUnavailable as exc:
            logger.exception("durable usage spool failed after inference completed")
            raise UpstreamUnavailable(
                "usage_spool_unavailable",
                "This stamp could not durably record completed usage",
            ) from exc


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


def _pick_available_auto_deployment(
    plane: DataPlane, candidates: list[Deployment]
) -> Deployment:
    if not candidates:
        raise NotFound(
            "auto_model_not_found",
            "No deployment available to this account matches the request",
        )
    for candidate in candidates:
        if plane.pool_for(candidate).has_available():
            return candidate
    raise UpstreamUnavailable(
        "upstream_unavailable",
        "Matching models exist, but no healthy model host is available",
    )


def _resolve_json_deployment(
    plane: DataPlane,
    *,
    model: str,
    account_id: uuid.UUID,
    payload: dict[str, Any],
    upstream_path: str,
) -> Deployment:
    if model != AUTO_MODEL:
        return plane.registry.resolve(model, account_id=account_id)

    owned = plane.registry.for_account(account_id)
    # Rolling compatibility: a real deployment already named "auto" keeps its exact
    # alias semantics. The virtual selector is used only when the account has no such
    # deployment.
    concrete = next((item for item in owned if item.model_alias == AUTO_MODEL), None)
    if concrete is not None:
        return concrete

    try:
        profile = profile_for_json(_JSON_OPERATIONS[upstream_path], payload)
    except ValueError as exc:
        raise BadRequest("invalid_routing", str(exc)) from exc
    # ``routing`` is a Fabric extension accepted through OpenAI SDK extra_body. Model
    # hosts do not know it and may reject unknown fields, so consume it at the router.
    payload.pop("routing", None)
    return _pick_available_auto_deployment(plane, rank_deployments(owned, profile))


def _resolve_audio_deployment(
    plane: DataPlane,
    *,
    model: str,
    account_id: uuid.UUID,
    upstream_path: str,
) -> Deployment:
    if model != AUTO_MODEL:
        return plane.registry.resolve(model, account_id=account_id)

    owned = plane.registry.for_account(account_id)
    concrete = next((item for item in owned if item.model_alias == AUTO_MODEL), None)
    if concrete is not None:
        return concrete
    profile = profile_for_audio(_AUDIO_OPERATIONS[upstream_path])
    return _pick_available_auto_deployment(plane, rank_deployments(owned, profile))


def _safe_to_retry_transport_failure(exc: httpx.HTTPError) -> bool:
    """Whether the request is known not to have reached a model host.

    A completion POST is not idempotent: replaying after a read/write/protocol failure
    can run and bill it twice because the first host may already be decoding. Only
    connection establishment failures are transparently retried on another backend.
    """
    return isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)


_SESSION_HEADERS = ("x-fabric-session", "x-session-id", "x-conversation-id")


def _session_key(request: Request, payload: dict[str, Any]) -> str | None:
    """Return an explicit affinity key, or None so anonymous traffic still spreads."""
    for header in _SESSION_HEADERS:
        value = request.headers.get(header)
        if value and value.strip():
            return value.strip()
    user = payload.get("user")
    if isinstance(user, str) and user.strip():
        return user.strip()
    return None


def _sync_backend_health(
    plane: DataPlane, pool: BackendPool, deployment_label: str
) -> None:
    for backend_id, state in pool.health.snapshot().items():
        plane.metrics.backend_health(
            deployment=deployment_label,
            backend=backend_id,
            available=bool(state["available"]),
        )


def _record_backend_failure(
    plane: DataPlane,
    pool: BackendPool,
    backend: Backend,
    deployment_label: str,
) -> None:
    if pool.health.record_failure(backend):
        plane.metrics.backend_ejected(
            deployment=deployment_label, backend=backend.backend_id
        )
    _sync_backend_health(plane, pool, deployment_label)


def _acquire_backend(
    plane: DataPlane,
    pool: BackendPool,
    backend: Backend,
    deployment_label: str,
) -> None:
    pool.health.acquire(backend)
    plane.metrics.backend_started(
        deployment=deployment_label, backend=backend.backend_id
    )
    _sync_backend_health(plane, pool, deployment_label)


def _release_backend(
    plane: DataPlane,
    pool: BackendPool,
    backend: Backend,
    deployment_label: str,
    outcome: str,
) -> None:
    pool.health.release(backend)
    plane.metrics.backend_finished(
        deployment=deployment_label,
        backend=backend.backend_id,
        outcome=outcome,
    )
    _sync_backend_health(plane, pool, deployment_label)


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
    # Retire withdrawn pool/metric state before resolution, including the case where
    # this request names the removed deployment and resolution itself will fail.
    plane.reconcile_pools()
    deployment = _resolve_json_deployment(
        plane,
        model=model,
        account_id=principal.account_id,
        payload=payload,
        upstream_path=upstream_path,
    )

    # A failed durable spool is known before another expensive model invocation. The request that
    # first discovers an I/O fault may already have completed upstream, but every later request
    # fails here rather than repeatedly spending GPU work that cannot be accounted for.
    if not plane.usage.healthy:
        raise UpstreamUnavailable(
            "usage_spool_unavailable",
            "This stamp cannot durably record usage",
        )

    # Checked after authorization so an unauthenticated or unauthorized caller cannot consume
    # another account's allowance, and before proxying so a refused request never reaches the GPU.
    # In production this is one atomic stamp-local coordinator decision shared by every gateway.
    try:
        admission = await plane.limits.acquire(principal.account_id)
    except LimitCoordinatorUnavailable as exc:
        raise UpstreamUnavailable(
            "limit_coordinator_unavailable",
            "This stamp cannot make a shared admission decision",
        ) from exc

    if not admission.allowed:
        reason = admission.reason or "limit_rejected"
        plane.metrics.refused(
            deployment=str(deployment.deployment_id),
            account=str(principal.account_id),
            reason=reason,
        )
        if reason == "rate_limited":
            raise TooManyRequests(
                "rate_limited",
                "This account has exceeded its request rate on this stamp",
                retry_after=max(1, math.ceil(admission.retry_after or 1.0)),
            )
        raise TooManyRequests(
            "too_many_in_flight",
            "This account has too many requests in flight on this stamp",
            retry_after=1,
        )

    streaming = bool(payload.get("stream"))
    upstream_payload = {**payload, "model": deployment.upstream_model_name}
    client_wants_usage_frame = False
    if streaming:
        # Ask the host to report its own token counts. Without this a streamed request is
        # unmetered, and streaming is what chat clients do by default (M6).
        upstream_payload, client_wants_usage_frame = streaming_upstream_payload(
            upstream_payload
        )
    headers = forwardable_headers(dict(request.headers))

    # A deployment is served by a pool of backends (ADR 0010). The pool skips a backend
    # that is ejected or unhealthy, so a healthy replica is chosen even when one is down.
    pool = plane.pool_for(deployment)
    session_key = _session_key(request, payload)

    deployment_label = str(deployment.deployment_id)
    account_label = str(principal.account_id)
    plane.metrics.request_started(deployment_label)

    if not streaming:
        request_recorded = False
        try:
            tried: set[str] = set()
            response: httpx.Response | None = None
            max_attempts = min(plane.settings.backend_max_attempts, len(pool))
            for _ in range(max_attempts):
                backend = pool.select(exclude=tried, session_key=session_key)
                if backend is None:
                    break

                _acquire_backend(plane, pool, backend, deployment_label)
                attempt_outcome = "cancelled"
                retry = False
                try:
                    response = await plane.client.post(
                        f"{backend.url}{upstream_path}",
                        json=upstream_payload,
                        headers=headers,
                        timeout=plane.settings.upstream_timeout_seconds,
                    )
                except httpx.HTTPError as exc:
                    logger.warning("backend %s failed: %s", backend.backend_id, exc)
                    _record_backend_failure(plane, pool, backend, deployment_label)
                    attempt_outcome = "transport_error"
                    if _safe_to_retry_transport_failure(exc):
                        tried.add(backend.backend_id)
                        response = None
                        retry = True
                    else:
                        plane.metrics.request_finished(
                            deployment=deployment_label,
                            account=account_label,
                            outcome="upstream_unavailable",
                            duration_seconds=time.perf_counter() - started,
                        )
                        request_recorded = True
                        raise UpstreamUnavailable(
                            "upstream_unavailable",
                            "The model host connection failed after the request may have started",
                        ) from exc
                else:
                    if response.status_code >= 500:
                        _record_backend_failure(plane, pool, backend, deployment_label)
                    else:
                        # A 4xx proves the host answered, so it is healthy even though
                        # the inference attempt itself did not succeed.
                        pool.health.record_success(backend)
                        _sync_backend_health(plane, pool, deployment_label)
                    attempt_outcome = "ok" if response.is_success else "upstream_error"
                finally:
                    _release_backend(
                        plane,
                        pool,
                        backend,
                        deployment_label,
                        attempt_outcome,
                    )

                if retry:
                    continue
                break

            if response is None:
                plane.metrics.request_finished(
                    deployment=deployment_label,
                    account=account_label,
                    outcome="upstream_unavailable",
                    duration_seconds=time.perf_counter() - started,
                )
                request_recorded = True
                raise UpstreamUnavailable(
                    "upstream_unavailable", "No healthy model host is available"
                )

            try:
                body = response.json()
            except ValueError:
                body = {
                    "error": {"code": "upstream_invalid_response", "message": "Non-JSON reply"}
                }

            usage = body.get("usage") if isinstance(body, dict) else None
            if response.is_success and isinstance(body, dict):
                plane.record_usage(principal, deployment, body, streamed=False)
                body = {**body, "model": deployment.model_alias}
            plane.metrics.request_finished(
                deployment=deployment_label,
                account=account_label,
                outcome="ok" if response.is_success else "upstream_error",
                duration_seconds=time.perf_counter() - started,
                input_tokens=int((usage or {}).get("prompt_tokens") or 0),
                output_tokens=int((usage or {}).get("completion_tokens") or 0),
            )
            request_recorded = True
            return JSONResponse(status_code=response.status_code, content=body)
        finally:
            if not request_recorded:
                plane.metrics.request_finished(
                    deployment=deployment_label,
                    account=account_label,
                    outcome="cancelled",
                    duration_seconds=time.perf_counter() - started,
                )
            await plane.limits.release(admission)

    if not pool.has_available():
        await plane.limits.release(admission)
        plane.metrics.request_finished(
            deployment=deployment_label,
            account=account_label,
            outcome="upstream_unavailable",
            duration_seconds=time.perf_counter() - started,
        )
        raise UpstreamUnavailable("upstream_unavailable", "No healthy model host is available")

    plane.retain_stream(deployment)
    cleanup_accounted = False
    cleanup_error: UpstreamUnavailable | None = None
    placement_released = False
    concurrency_release: asyncio.Task[None] | None = None
    request_outcome = "cancelled_streamed"
    meter = UsageMeter(forward_usage_frame=client_wants_usage_frame)
    #: Whether a host accepted the request. Usage is only trusted from an accepted stream, which
    #: is the same rule the non-streaming path applies with ``response.is_success``.
    upstream_ok = False

    async def cleanup_stream() -> None:
        """Account once and release both leases, even if disconnect cancellation interrupts.

        Accounting and placement release are synchronous phases and therefore complete before the
        only cancellable operation. Concurrency release runs in one shielded task: if generator
        cleanup is cancelled while it waits on the limiter lock, the response wrapper can await
        the same task without duplicating usage, metrics, or decrementing the limiter twice.
        """
        nonlocal cleanup_accounted, cleanup_error, placement_released, concurrency_release
        if not cleanup_accounted:
            # Only from a stream the host accepted, which is the rule the non-streaming path
            # applies with response.is_success. The meter itself withholds usage it cannot vouch
            # for, so this is the one condition left to check.
            usage = meter.usage if upstream_ok else None
            if usage is not None:
                # Real numbers for a request the host accepted. A persistence fault is retained
                # as the cleanup result, but cannot skip request/placement/concurrency release.
                try:
                    plane.record_usage(principal, deployment, usage.as_payload(), streamed=True)
                except UpstreamUnavailable as exc:
                    cleanup_error = exc
            # Every accepted stream reports here, lost count or not. A zero-token row is
            # indistinguishable from a real answer that cost nothing, so a loss is counted instead.
            plane.metrics.stream_finished(
                deployment=deployment_label,
                account=account_label,
                unmetered_reason=meter.unmetered_reason if upstream_ok else None,
            )
            plane.metrics.request_finished(
                deployment=deployment_label,
                account=account_label,
                outcome=request_outcome,
                duration_seconds=time.perf_counter() - started,
                input_tokens=usage.input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
            )
            cleanup_accounted = True

        # Synchronous and deliberately before the limiter await. Withdrawal must not remain held
        # merely because disconnect cancellation arrived while asyncio.Lock was contended.
        if not placement_released:
            plane.release_stream(deployment)
            placement_released = True

        if concurrency_release is None:
            concurrency_release = asyncio.create_task(
                plane.limits.release(admission)
            )
        await asyncio.shield(concurrency_release)
        if cleanup_error is not None:
            raise cleanup_error

    async def stream() -> AsyncIterator[bytes]:
        nonlocal request_outcome, upstream_ok
        tried: set[str] = set()
        backend = pool.select(session_key=session_key)
        max_attempts = min(plane.settings.backend_max_attempts, len(pool))
        try:
            for attempt in range(max_attempts):
                if backend is None:
                    break
                _acquire_backend(plane, pool, backend, deployment_label)
                # Received, not forwarded. A byte that arrived proves this host began work on a
                # non-idempotent completion, whether or not the gateway passed it on, so it is
                # what decides that a retry would replay real work (ADR 0011).
                received_any = False
                retry = False
                attempt_outcome = "cancelled"
                # Per attempt, like the meter: a previous attempt's acceptance says nothing
                # about this one.
                upstream_ok = False
                # A retried attempt is a different upstream stream; nothing from the last one
                # may bleed into its framing or its usage.
                meter.reset()
                try:
                    async with plane.client.stream(
                        "POST",
                        f"{backend.url}{upstream_path}",
                        json=upstream_payload,
                        headers=headers,
                        timeout=plane.settings.upstream_timeout_seconds,
                    ) as upstream:
                        health_failed = upstream.status_code >= 500
                        upstream_error = not upstream.is_success
                        upstream_ok = upstream.is_success
                        async for chunk in upstream.aiter_bytes():
                            if chunk:
                                received_any = True
                            forward = meter.feed(chunk)
                            if forward:
                                yield forward
                        # The upstream body was read to its end, so a running subtotal is now
                        # this stream's total.
                        trailing = meter.finish(complete=True)
                        if trailing:
                            yield trailing
                    if health_failed:
                        _record_backend_failure(plane, pool, backend, deployment_label)
                    else:
                        pool.health.record_success(backend)
                        _sync_backend_health(plane, pool, deployment_label)
                    attempt_outcome = "upstream_error" if upstream_error else "ok"
                    request_outcome = (
                        "upstream_error_streamed" if upstream_error else "ok_streamed"
                    )
                    return
                except httpx.HTTPError as exc:
                    logger.warning("backend %s stream failed: %s", backend.backend_id, exc)
                    _record_backend_failure(plane, pool, backend, deployment_label)
                    attempt_outcome = "transport_error"
                    if (
                        not received_any
                        and _safe_to_retry_transport_failure(exc)
                        and attempt + 1 < max_attempts
                    ):
                        tried.add(backend.backend_id)
                        retry = True
                    else:
                        request_outcome = "upstream_error_streamed"
                        # Whatever was framed but not yet emitted still belongs to the client,
                        # exactly as it did under the plain byte relay. Not complete: the body
                        # stopped mid-stream, so anything counted so far is a subtotal.
                        trailing = meter.finish(complete=False)
                        if trailing:
                            yield trailing
                        yield b'data: {"error":{"code":"upstream_unavailable"}}\n\n'
                        return
                finally:
                    _release_backend(
                        plane,
                        pool,
                        backend,
                        deployment_label,
                        attempt_outcome,
                    )

                if retry:
                    backend = pool.select(exclude=tried, session_key=session_key)
                    continue

            request_outcome = "upstream_error_streamed"
            yield b'data: {"error":{"code":"upstream_unavailable"}}\n\n'
        finally:
            await cleanup_stream()

    # Usage is recorded in cleanup_stream, once the stream has actually reported it.
    return _CleanupStreamingResponse(stream(), cleanup=cleanup_stream)


#: Form fields never taken from the caller. ``model`` is replaced with the deployment's
#: release so a caller cannot reach another model by naming it, and ``file`` is re-sent as
#: the upload rather than as a text field.
_TRANSCRIPTION_RESERVED_FIELDS = frozenset({"model", "file"})


async def _proxy_transcription(
    plane: DataPlane,
    request: Request,
    upstream_path: str,
) -> Response:
    """Authenticate, authorize, then forward a multipart audio request.

    Deliberately separate from :func:`_proxy` rather than generalising it. Four things
    differ: the body is multipart rather than JSON, the model arrives as a form field,
    the reply may be plain text rather than an object, and there is no streaming
    variant. Folding all four into the JSON path would complicate the route every
    completion takes, which is the hotter and more heavily tested one.

    What is *not* different is the order: authenticate, resolve ownership, refuse if
    usage cannot be recorded, admit against the account's limits, and only then spend
    GPU time. A refused request never reaches a host.
    """
    started = time.perf_counter()
    principal = plane.authenticate(request.headers.get("authorization"))

    try:
        form = await request.form()
    except Exception as exc:  # starlette raises assorted parse errors
        raise BadRequest(
            "invalid_multipart", "Request body is not valid multipart/form-data"
        ) from exc

    try:
        model = form.get("model")
        if not isinstance(model, str) or not model.strip():
            raise BadRequest("model_required", "A model must be specified")
        model = model.strip()

        upload = form.get("file")
        if not hasattr(upload, "read"):
            raise BadRequest("file_required", "An audio file must be supplied as 'file'")
        audio = await upload.read(plane.settings.max_audio_upload_bytes + 1)
        if not audio:
            raise BadRequest("file_empty", "The supplied audio file is empty")
        if len(audio) > plane.settings.max_audio_upload_bytes:
            raise PayloadTooLarge(
                "audio_too_large",
                "The supplied audio file exceeds the configured size limit",
                max_bytes=plane.settings.max_audio_upload_bytes,
            )

        # Refused rather than silently downgraded: a caller that asked for incremental
        # transcription and received one final object would have no way to tell.
        stream_field = form.get("stream")
        if isinstance(stream_field, str) and stream_field.strip().lower() in ("1", "true"):
            raise BadRequest(
                "stream_unsupported",
                "This endpoint does not stream; omit 'stream' to receive one response",
            )

        extra_fields = {
            name: value
            for name, value in form.multi_items()
            if isinstance(value, str) and name not in _TRANSCRIPTION_RESERVED_FIELDS
        }
        filename = getattr(upload, "filename", None) or "audio"
        content_type = getattr(upload, "content_type", None) or "application/octet-stream"
    finally:
        # Starlette spools large uploads to disk; the temporary file is ours to close.
        await form.close()

    # Ownership comes from the verified token plus local configuration; the model name
    # only selects a candidate.
    plane.reconcile_pools()
    deployment = _resolve_audio_deployment(
        plane,
        model=model,
        account_id=principal.account_id,
        upstream_path=upstream_path,
    )

    if not plane.usage.healthy:
        raise UpstreamUnavailable(
            "usage_spool_unavailable",
            "This stamp cannot durably record usage",
        )

    try:
        admission = await plane.limits.acquire(principal.account_id)
    except LimitCoordinatorUnavailable as exc:
        raise UpstreamUnavailable(
            "limit_coordinator_unavailable",
            "This stamp cannot make a shared admission decision",
        ) from exc

    deployment_label = str(deployment.deployment_id)
    account_label = str(principal.account_id)

    if not admission.allowed:
        reason = admission.reason or "limit_rejected"
        plane.metrics.refused(
            deployment=deployment_label, account=account_label, reason=reason
        )
        if reason == "rate_limited":
            raise TooManyRequests(
                "rate_limited",
                "This account has exceeded its request rate on this stamp",
                retry_after=max(1, math.ceil(admission.retry_after or 1.0)),
            )
        raise TooManyRequests(
            "too_many_in_flight",
            "This account has too many requests in flight on this stamp",
            retry_after=1,
        )

    headers = forwardable_headers(dict(request.headers))
    # httpx generates its own multipart boundary, so the client's content-type would
    # describe a boundary that is no longer in the body.
    headers.pop("content-type", None)
    headers.pop("Content-Type", None)

    pool = plane.pool_for(deployment)
    plane.metrics.request_started(deployment_label)
    request_recorded = False
    try:
        tried: set[str] = set()
        response: httpx.Response | None = None
        max_attempts = min(plane.settings.backend_max_attempts, len(pool))
        for _ in range(max_attempts):
            backend = pool.select(exclude=tried, session_key=None)
            if backend is None:
                break

            _acquire_backend(plane, pool, backend, deployment_label)
            attempt_outcome = "cancelled"
            retry = False
            try:
                response = await plane.client.post(
                    f"{backend.url}{upstream_path}",
                    data={**extra_fields, "model": deployment.upstream_model_name},
                    files={"file": (filename, audio, content_type)},
                    headers=headers,
                    timeout=plane.settings.upstream_timeout_seconds,
                )
            except httpx.HTTPError as exc:
                logger.warning("backend %s failed: %s", backend.backend_id, exc)
                _record_backend_failure(plane, pool, backend, deployment_label)
                attempt_outcome = "transport_error"
                # Transcription is as non-idempotent as completion: the first host may
                # already be decoding, so only a failed connection is replayed.
                if _safe_to_retry_transport_failure(exc):
                    tried.add(backend.backend_id)
                    response = None
                    retry = True
                else:
                    plane.metrics.request_finished(
                        deployment=deployment_label,
                        account=account_label,
                        outcome="upstream_unavailable",
                        duration_seconds=time.perf_counter() - started,
                    )
                    request_recorded = True
                    raise UpstreamUnavailable(
                        "upstream_unavailable",
                        "The model host connection failed after the request may have started",
                    ) from exc
            else:
                if response.status_code >= 500:
                    _record_backend_failure(plane, pool, backend, deployment_label)
                else:
                    pool.health.record_success(backend)
                    _sync_backend_health(plane, pool, deployment_label)
                attempt_outcome = "ok" if response.is_success else "upstream_error"
            finally:
                _release_backend(
                    plane, pool, backend, deployment_label, attempt_outcome
                )

            if retry:
                continue
            break

        if response is None:
            plane.metrics.request_finished(
                deployment=deployment_label,
                account=account_label,
                outcome="upstream_unavailable",
                duration_seconds=time.perf_counter() - started,
            )
            request_recorded = True
            raise UpstreamUnavailable(
                "upstream_unavailable", "No healthy model host is available"
            )

        # ``response_format=text`` and the subtitle formats reply with a media type that
        # is not JSON at all, so the body is only interpreted when it claims to be JSON.
        upstream_media = response.headers.get("content-type", "")
        body: Any = None
        if "json" in upstream_media.lower():
            try:
                body = response.json()
            except ValueError:
                body = None

        usage = body.get("usage") if isinstance(body, dict) else None
        if response.is_success and isinstance(body, dict):
            plane.record_usage(principal, deployment, body, streamed=False)
            # The caller's alias, never the internal release. Only set when the host
            # reported a model, so a reply that carries none is not given one.
            if "model" in body:
                body = {**body, "model": deployment.model_alias}
        plane.metrics.request_finished(
            deployment=deployment_label,
            account=account_label,
            outcome="ok" if response.is_success else "upstream_error",
            duration_seconds=time.perf_counter() - started,
            input_tokens=int((usage or {}).get("prompt_tokens") or 0),
            output_tokens=int((usage or {}).get("completion_tokens") or 0),
        )
        request_recorded = True
        if body is not None:
            return JSONResponse(status_code=response.status_code, content=body)
        return Response(
            status_code=response.status_code,
            content=response.content,
            media_type=upstream_media or "text/plain",
        )
    finally:
        if not request_recorded:
            plane.metrics.request_finished(
                deployment=deployment_label,
                account=account_label,
                outcome="cancelled",
                duration_seconds=time.perf_counter() - started,
            )
        await plane.limits.release(admission)


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
        plane.reconcile_pools()
        return Response(content=plane.metrics.render(), media_type="text/plain; version=0.0.4")

    @router.post(CHAT_COMPLETIONS_PATH, summary="Chat completion")
    async def chat_completions(request: Request):
        return await _proxy(plane, request, CHAT_COMPLETIONS_PATH)

    @router.post(COMPLETIONS_PATH, summary="Text completion")
    async def completions(request: Request):
        return await _proxy(plane, request, COMPLETIONS_PATH)

    @router.post(TRANSCRIPTIONS_PATH, summary="Transcribe audio to text")
    async def transcriptions(request: Request):
        return await _proxy_transcription(plane, request, TRANSCRIPTIONS_PATH)

    @router.post(TRANSLATIONS_PATH, summary="Translate audio to English text")
    async def translations(request: Request):
        return await _proxy_transcription(plane, request, TRANSLATIONS_PATH)

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
        spool = plane.usage.snapshot()
        limits = await plane.limits.snapshot()
        ready = keys["keys_held"] > 0 and spool["healthy"] and limits["healthy"]
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ready" if ready else "unavailable",
            "keys_held": keys["keys_held"],
            "deployments": len(plane.registry),
            "usage_spool_healthy": spool["healthy"],
            "limit_coordinator_healthy": limits["healthy"],
        }

    @router.get("/admin/keys", summary="Verification key cache state")
    async def key_state() -> dict[str, Any]:
        return plane.keys.snapshot()

    @router.get("/admin/verification", summary="What this gateway enforces on tokens")
    async def verification_state() -> dict[str, Any]:
        """The verification contract this process actually applies.

        Reported rather than assumed from the chart: a gateway is configured at startup
        from its environment, so the only authority on what it enforces is the process
        itself. A stamp whose issuer does not match the control plane that mints its
        tokens rejects everything with a generic code, and reading this is how that is
        told apart from a genuinely bad caller.
        """
        settings = plane.settings
        return {
            # From the policy, not the settings: the control plane's own identity wins
            # over the install's, so reading settings here would report a value this
            # process may no longer be enforcing.
            **plane.verification.snapshot(),
            # Constants rather than settings, reported so an operator does not have to
            # read the source to know what a token must carry.
            "audience": INFERENCE_AUDIENCE,
            "required_scope": INFERENCE_SCOPE,
            "leeway_seconds": settings.leeway_seconds,
            "jwks_refresh_seconds": settings.jwks_refresh_seconds,
            # Whether a cold start during a control-plane outage can verify at all.
            "jwks_file_seeded": bool(settings.load_jwks_file()),
        }

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
        return await plane.limits.snapshot()

    @router.get("/admin/usage", summary="Local durable usage spool state")
    async def usage_state() -> dict[str, Any]:
        return plane.usage.snapshot()

    @router.post("/admin/usage/drain", summary="Lease buffered usage for forwarding")
    async def drain_usage(limit: int = 500) -> dict[str, Any]:
        """Lease a stable batch without removing it from the durable spool.

        The historical route name is retained for rolling compatibility, but this is no longer
        destructive. A collector that restarts or loses a response receives the same lease and
        safely replays the same record IDs. Records leave only through the acknowledgement route.
        """
        if limit < 1 or limit > 500:
            raise BadRequest("invalid_usage_lease_limit", "limit must be between 1 and 500")
        try:
            lease = plane.usage.lease(limit)
        except UsageSpoolUnavailable as exc:
            raise UpstreamUnavailable(
                "usage_spool_unavailable", "The durable usage spool cannot be leased"
            ) from exc
        return {
            "lease_id": str(lease.lease_id) if lease.lease_id else None,
            "records": [record.as_dict() for record in lease.records],
            "count": len(lease.records),
        }

    @router.post("/admin/usage/ack", summary="Acknowledge a resolved usage lease")
    async def acknowledge_usage(request: Request) -> dict[str, Any]:
        """Delete a lease only after the control plane resolved every record in it."""
        payload = await _read_json(request)
        raw_lease_id = payload.get("lease_id")
        expected_count = payload.get("expected_count")
        try:
            lease_id = uuid.UUID(raw_lease_id) if isinstance(raw_lease_id, str) else None
        except ValueError as exc:
            raise BadRequest("invalid_usage_lease", "lease_id must be a UUID") from exc
        if lease_id is None:
            raise BadRequest("invalid_usage_lease", "lease_id must be a UUID")
        if (
            isinstance(expected_count, bool)
            or not isinstance(expected_count, int)
            or expected_count < 1
            or expected_count > 500
        ):
            raise BadRequest(
                "invalid_usage_lease_count", "expected_count must be between 1 and 500"
            )
        try:
            result = plane.usage.acknowledge(lease_id, expected_count)
        except UsageLeaseMismatch as exc:
            raise ApiError(
                409,
                "usage_lease_mismatch",
                "The acknowledgement does not match the outstanding usage lease",
            ) from exc
        except UsageSpoolUnavailable as exc:
            raise UpstreamUnavailable(
                "usage_spool_unavailable", "The durable usage lease cannot be acknowledged"
            ) from exc
        return {
            "lease_id": str(lease_id),
            "acknowledged": result.acknowledged,
            "deleted": result.deleted,
            "already_acknowledged": result.already_acknowledged,
        }

    return router


def build_limit_coordinator_router(
    store: LimitCoordinatorStore,
    token: str,
) -> APIRouter:
    """Private stamp-local authority for atomic cross-replica admission."""
    router = APIRouter(tags=["limits"])

    def require_token(authorization: str | None) -> None:
        if not limit_authorized(authorization, token):
            raise ApiError(401, "invalid_limit_credential", "Invalid limit coordinator token")

    @router.get("/healthz", summary="Limit coordinator liveness")
    async def limit_health(response: Response) -> dict[str, Any]:
        if not store.healthy:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "ok" if store.healthy else "unavailable"}

    @router.post("/limits/admit", summary="Atomically apply shared account limits")
    async def limit_admit(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(authorization)
        payload = await _read_json(request)
        try:
            account_id = uuid.UUID(str(payload.get("account_id")))
            request_id = uuid.UUID(str(payload.get("request_id")))
        except ValueError as exc:
            raise BadRequest(
                "invalid_limit_identity", "account_id and request_id must be UUIDs"
            ) from exc
        try:
            decision = await store.acquire(account_id, request_id)
        except ValueError as exc:
            raise BadRequest(
                "invalid_limit_request", "request_id was already used incompatibly"
            ) from exc
        except LimitCoordinatorUnavailable as exc:
            raise UpstreamUnavailable(
                "limit_coordinator_unavailable", "Shared limit state is unavailable"
            ) from exc
        return {
            "allowed": decision.allowed,
            "reason": decision.reason,
            "retry_after": decision.retry_after,
            "lease_id": str(decision.lease_id) if decision.lease_id else None,
        }

    @router.post("/limits/renew", summary="Renew one shared concurrency lease")
    async def limit_renew(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(authorization)
        payload = await _read_json(request)
        try:
            lease_id = uuid.UUID(str(payload.get("lease_id")))
        except ValueError as exc:
            raise BadRequest("invalid_limit_lease", "lease_id must be a UUID") from exc
        try:
            renewed = await store.renew(lease_id)
        except LimitCoordinatorUnavailable as exc:
            raise UpstreamUnavailable(
                "limit_coordinator_unavailable", "Shared limit state is unavailable"
            ) from exc
        return {"lease_id": str(lease_id), "renewed": renewed}

    @router.post("/limits/release", summary="Release one shared concurrency lease")
    async def limit_release(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(authorization)
        payload = await _read_json(request)
        try:
            lease_id = uuid.UUID(str(payload.get("lease_id")))
        except ValueError as exc:
            raise BadRequest("invalid_limit_lease", "lease_id must be a UUID") from exc
        try:
            released = await store.release(lease_id)
        except LimitCoordinatorUnavailable as exc:
            raise UpstreamUnavailable(
                "limit_coordinator_unavailable", "Shared limit state is unavailable"
            ) from exc
        return {"lease_id": str(lease_id), "released": released}

    @router.get("/limits/state", summary="Authoritative shared limit state")
    async def shared_limit_state(
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(authorization)
        try:
            return await store.snapshot()
        except LimitCoordinatorUnavailable as exc:
            raise UpstreamUnavailable(
                "limit_coordinator_unavailable", "Shared limit state is unavailable"
            ) from exc

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
    # Built here so the key cache and the request path share one, seeded from this
    # install's values and replaced by the control plane's on the first desired-state pass.
    policy = VerificationPolicy(resolved)
    # A caller-supplied cache keeps its own policy if it has one, so an injected cache is
    # never quietly pointed at a key source it was not built for.
    cache = keys if keys is not None else KeyCache(resolved, policy=policy)
    return DataPlane(
        settings=resolved,
        keys=cache,
        policy=getattr(cache, "policy", None) or policy,
        # Follows the file the agent rewrites rather than reading it once.
        registry=ReloadingRegistry(resolved.deployments_file),
        client=httpx.AsyncClient(**tls),
    )


def _probe_router(plane: DataPlane) -> APIRouter:
    """Health probes on the inference listener.

    These exist here as well as on the administrative listener because the
    administrative listener binds to localhost only, so a Kubernetes kubelet
    probing the pod's address cannot reach it. Binding that listener wider would expose
    usage leasing, acknowledgement, and internal state to the whole cluster, so the
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
        limits = await plane.limits.snapshot()
        if (
            plane.keys.snapshot()["keys_held"] == 0
            or not plane.usage.healthy
            or not limits["healthy"]
        ):
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


def create_limit_coordinator_app(plane: DataPlane | None = None) -> FastAPI:
    """Private stamp-local shared-limit authority."""
    resolved = plane or build_plane()
    if resolved.limit_store is None:
        raise ValueError("limit coordinator store path is not configured")
    token = resolved.settings.limit_coordinator_token
    if not token:
        raise ValueError("limit coordinator token is not configured")
    app = FastAPI(title="Fabric Limit Coordinator", version="0.1.0")
    app.state.plane = resolved
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(build_limit_coordinator_router(resolved.limit_store, token))
    return app


def create_router_status_app(plane: DataPlane | None = None) -> FastAPI:
    """Private non-destructive listener used only for rollout acknowledgement."""
    resolved = plane or build_plane()
    app = FastAPI(title="Fabric Router Status", version="0.1.0")
    app.state.plane = resolved

    @app.get("/router-state")
    async def router_state() -> dict[str, Any]:
        return resolved.router_state()

    return app


def create_admin_app(plane: DataPlane | None = None) -> FastAPI:
    """Internal listener. Must not be exposed alongside inference (AR-DP03)."""
    resolved = plane or build_plane()
    app = FastAPI(title="Fabric Data Plane Admin", version="0.1.0")
    app.state.plane = resolved
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(build_admin_router(resolved))
    return app
