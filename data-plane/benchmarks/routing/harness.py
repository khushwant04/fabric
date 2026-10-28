"""Run one workload through the real gateway against a simulated fleet.

The gateway is the production ``create_inference_app`` served by uvicorn on a local port,
and the load generator is an HTTP client, so a request crosses a real socket and the
streaming path the way a customer's does. (``httpx``'s in-process ASGI transport buffers a
whole response before returning it, which would make time-to-first-token meaningless.)
Only the gateway's upstream connection is in-process, pointing at the simulated hosts.

Simulated time runs ``time_scale`` times faster than wall time, so a minute of load takes
fifteen seconds at the default. Latencies are measured on the wall clock and divided by
the scale. The gateway's own processing is not simulated, so it is scaled up too; the
calibration run measures it, and the report states it beside the results.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import datetime as dt
import hashlib
import json
import socket
import time
import uuid
from typing import Any

import httpx
import jwt
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from benchmarks.routing.simhost import REPLY_FIELD, Fleet, HostModel, SimulatedHost
from benchmarks.routing.workload import Turn, WorkloadConfig, generate
from fabric_data_plane.app import DataPlane, create_inference_app
from fabric_data_plane.config import Settings
from fabric_data_plane.keys import KeyCache
from fabric_data_plane.pool import Backend
from fabric_data_plane.registry import Deployment, DeploymentRegistry

ISSUER = "https://control.bench.test"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
ACCOUNT = uuid.UUID("0b0b0b0b-0b0b-0b0b-0b0b-0b0b0b0b0b0b")
DEPLOYMENT = uuid.UUID("0d0d0d0d-0d0d-0d0d-0d0d-0d0d0d0d0d0d")
MODEL_ALIAS = "bench-model"


@dataclasses.dataclass(frozen=True)
class RunConfig:
    strategy: str
    workload: WorkloadConfig
    backends: int = 4
    host: HostModel = HostModel()
    #: Wall seconds per simulated second.
    time_scale: float = 0.25
    #: Simulated seconds after which an unfinished request counts as failed.
    request_timeout_seconds: float = 300.0


@dataclasses.dataclass(frozen=True)
class RequestResult:
    session_id: str
    turn: int
    #: Simulated seconds since the start of the run.
    started: float
    status: int
    ttft: float | None
    latency: float | None
    prompt_tokens: int
    output_tokens: int
    cached_tokens: int
    backend: str | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.error is None and self.ttft is not None


@dataclasses.dataclass
class RunResult:
    config: RunConfig
    requests: list[RequestResult]
    host_stats: dict[str, dict[str, int]]
    wall_seconds: float
    #: Simulated seconds from the first request to the last completion.
    span_seconds: float


class _Signer:
    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self._key.public_key().public_numbers()
        self.kid = hashlib.sha256(f"{numbers.n}:{numbers.e}".encode()).hexdigest()[:32]

        def b64(value: int) -> str:
            raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        self.jwks = {"keys": [{
            "kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.kid,
            "n": b64(numbers.n), "e": b64(numbers.e),
        }]}
        self._pem = self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def token(self) -> str:
        now = dt.datetime.now(tz=dt.UTC)
        claims = {
            "iss": ISSUER, "sub": str(uuid.uuid4()), "aud": "fabric-inference",
            "iat": int(now.timestamp()), "nbf": int(now.timestamp()),
            "exp": int((now + dt.timedelta(hours=6)).timestamp()),
            "jti": uuid.uuid4().hex, "account_id": str(ACCOUNT),
            "principal_type": "service", "scp": ["inference:invoke"],
        }
        return jwt.encode(claims, self._pem, algorithm="RS256", headers={"kid": self.kid})


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _turn_payload(turn: Turn, *, set_user: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": MODEL_ALIAS,
        "messages": [message.as_openai() for message in turn.messages],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": turn.output_tokens,
    }
    if set_user:
        payload["user"] = turn.session_id
    return payload


def _reply_tokens(turn: Turn) -> list[int]:
    """The reply the workload decided on, which the next turn resends as history."""
    following = turn.next_turn
    if following is None:
        return []
    for message in following.messages[len(turn.messages) :]:
        if message.role == "assistant":
            return list(message.tokens)
    return []


class _Gateway:
    """The production gateway, served on a local port, in front of the simulated fleet."""

    def __init__(self, config: RunConfig, fleet: Fleet, signer: _Signer) -> None:
        settings = Settings(
            app_env="test",
            jwt_issuer=ISSUER,
            jwks_url=JWKS_URL,
            deployments_file="does-not-exist.json",
            usage_buffer_size=1_000_000,
            upstream_timeout_seconds=config.request_timeout_seconds * config.time_scale,
        )
        keys = KeyCache(
            settings,
            client=httpx.Client(transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=signer.jwks)
            )),
        )
        deployment = Deployment(
            deployment_id=DEPLOYMENT,
            account_id=ACCOUNT,
            model_alias=MODEL_ALIAS,
            upstream_model="bench-release",
            backends=tuple(
                Backend(url=fleet.url(backend_id), backend_id=backend_id)
                for backend_id in fleet.hosts
            ),
            strategy=config.strategy,
        )
        self.plane = DataPlane(
            settings=settings,
            keys=keys,
            registry=DeploymentRegistry([deployment]),
            client=httpx.AsyncClient(
                transport=fleet.transport(),
                timeout=httpx.Timeout(None),
                limits=httpx.Limits(max_connections=None, max_keepalive_connections=None),
            ),
        )
        self.port = _free_port()
        self._server = uvicorn.Server(uvicorn.Config(
            create_inference_app(self.plane),
            host="127.0.0.1", port=self.port, log_level="warning", access_log=False,
            ws="none",  # the gateway serves no websockets; skip loading the library
        ))
        self._task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self) -> _Gateway:
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await self._task
        await self.plane.client.aclose()


async def _send(
    client: httpx.AsyncClient,
    turn: Turn,
    index: int,
    *,
    token: str,
    config: RunConfig,
    started_at: float,
) -> RequestResult:
    scale = config.time_scale
    payload = _turn_payload(turn, set_user=config.workload.set_user_field)
    payload[REPLY_FIELD] = _reply_tokens(turn)
    begin = time.perf_counter()
    first: float | None = None
    usage: dict[str, Any] = {}
    backend: str | None = None
    status = 0
    error: str | None = None
    try:
        async with client.stream(
            "POST", "/v1/chat/completions", json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=config.request_timeout_seconds * scale,
        ) as response:
            status = response.status_code
            if status != 200:
                body = (await response.aread()).decode(errors="replace")
                error = body[:200]
            else:
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    document = json.loads(line[6:])
                    if "error" in document:
                        error = json.dumps(document["error"])[:200]
                        continue
                    backend = document.get("system_fingerprint") or backend
                    if document.get("usage"):
                        usage = document["usage"]
                    choices = document.get("choices") or []
                    if first is None and choices and choices[0].get("delta", {}).get("content"):
                        first = time.perf_counter()
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}"[:200]
    end = time.perf_counter()
    details = usage.get("prompt_tokens_details") or {}
    return RequestResult(
        session_id=turn.session_id,
        turn=index,
        started=(begin - started_at) / scale,
        status=status,
        ttft=(first - begin) / scale if first is not None else None,
        latency=(end - begin) / scale if error is None else None,
        prompt_tokens=int(usage.get("prompt_tokens") or turn.prompt_tokens),
        output_tokens=int(usage.get("completion_tokens") or 0),
        cached_tokens=int(details.get("cached_tokens") or 0),
        backend=backend,
        error=error,
    )


async def run(config: RunConfig) -> RunResult:
    """Replay ``config.workload`` through the gateway and return every request's outcome."""
    scale = config.time_scale

    async def sleep(simulated: float) -> None:
        await asyncio.sleep(max(0.0, simulated) * scale)

    fleet = Fleet([
        SimulatedHost(f"backend-{index}", config.host, sleep) for index in range(config.backends)
    ])
    signer = _Signer()
    token = signer.token()
    sessions = generate(config.workload)
    results: list[RequestResult] = []

    async with _Gateway(config, fleet, signer) as gateway, httpx.AsyncClient(
        base_url=gateway.url,
        limits=httpx.Limits(max_connections=None, max_keepalive_connections=None),
    ) as client:
        started_at = time.perf_counter()

        async def session(first: Turn) -> None:
            await asyncio.sleep(max(0.0, first.offset * scale - (time.perf_counter() - started_at)))
            turn: Turn | None = first
            index = 0
            while turn is not None:
                result = await _send(
                    client, turn, index, token=token, config=config, started_at=started_at
                )
                results.append(result)
                if not result.ok:
                    return  # a person whose request failed does not send the follow-up
                await sleep(turn.think_seconds)
                turn = turn.next_turn
                index += 1

        await asyncio.gather(*(session(first) for first in sessions))
        wall = time.perf_counter() - started_at

    ends = [r.started + (r.latency or 0.0) for r in results]
    return RunResult(
        config=config,
        requests=sorted(results, key=lambda r: (r.started, r.session_id, r.turn)),
        host_stats={
            backend_id: dataclasses.asdict(host.stats) for backend_id, host in fleet.hosts.items()
        },
        wall_seconds=wall,
        span_seconds=max(ends, default=0.0),
    )


async def calibrate(*, requests: int = 50, time_scale: float = 0.25) -> dict[str, float]:
    """Measure the gateway's own time-to-first-byte against a host that takes no time.

    Whatever this reports is wall time the simulation does not model, so it is recorded
    beside every result rather than silently included in the strategies' numbers.
    """
    instant = HostModel(
        prefill_ms_per_token=0.0, prefill_overhead_ms=0.0,
        decode_ms_base=0.0, decode_ms_per_sequence=0.0,
    )
    config = RunConfig(
        strategy="round_robin",
        workload=WorkloadConfig(
            kind="shared_prefix", rate=requests / 5.0, duration_seconds=5.0,
            system_prompt_tokens=256, user_message_tokens=32, mean_output_tokens=8, seed=7,
        ),
        backends=2,
        host=instant,
        time_scale=1.0,
    )
    result = await run(config)
    wall_ms = sorted(r.ttft * 1000 for r in result.requests if r.ok and r.ttft is not None)
    if not wall_ms:
        return {"requests": 0}
    median = wall_ms[len(wall_ms) // 2]
    return {
        "requests": float(len(wall_ms)),
        "gateway_ttft_wall_ms_p50": round(median, 3),
        "gateway_ttft_wall_ms_p95": round(wall_ms[int(0.95 * (len(wall_ms) - 1))], 3),
        # What that wall time becomes on the simulated clock at the benchmark's scale.
        "gateway_ttft_simulated_ms_p50": round(median / time_scale, 3),
        "time_scale": time_scale,
    }

