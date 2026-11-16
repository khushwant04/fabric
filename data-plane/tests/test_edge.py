"""The edge gateway (ADR 0021), against in-process stamps and control plane.

Stamps are httpx MockTransport handlers keyed by host, so every test can assert which
stamp saw what, and that a refused request reached none of them.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest

from fabric_data_plane.edge.app import EdgeGateway, create_edge_admin_app, create_edge_app
from fabric_data_plane.edge.config import EdgeSettings
from fabric_data_plane.edge.poller import EdgeKeyCache
from fabric_data_plane.edge.table import RoutingTable, gateway_origin
from tests.conftest import ACCOUNT_A, ACCOUNT_B, ISSUER, JWKS_URL, SigningKey

ACCOUNT_C = uuid.UUID("33333333-3333-3333-3333-333333333333")
STAMP_A = "aaaaaaaa-0000-0000-0000-000000000001"
STAMP_B = "bbbbbbbb-0000-0000-0000-000000000002"
DEP_SHARED = uuid.UUID("d0000000-0000-0000-0000-000000000001")
DEP_ONLY_A = uuid.UUID("d0000000-0000-0000-0000-000000000002")
DEP_OTHER = uuid.UUID("d0000000-0000-0000-0000-000000000003")
DEP_C = uuid.UUID("d0000000-0000-0000-0000-000000000004")


def placement(stamp_id: str, name: str, region: str, *, ready: bool = True) -> dict[str, Any]:
    return {
        "stamp_id": stamp_id,
        "stamp_name": name,
        "region": region,
        "orchestrator": "k3s",
        "inference_url": f"https://{name}.stamps.example.com",
        "ready": ready,
        "phase": "ready" if ready else "pending",
        "ready_replicas": 1,
        "status_reported_at": None,
        "last_heartbeat_at": "2026-11-16T00:00:00Z",
        "heartbeat_age_seconds": 1.0,
        "gpus": [{"product": "GPU", "count": 1, "memory_bytes": 0}],
    }


def table_document(revision: int = 10, etag: str = '"v1"') -> dict[str, Any]:
    return {
        "revision": revision,
        "etag": etag,
        "generated_at": "2026-11-16T00:00:00Z",
        "stamp_liveness_seconds": 300,
        "deployments": [
            {
                "account_id": str(ACCOUNT_A),
                "deployment_id": str(DEP_SHARED),
                "name": "shared",
                "model_alias": "shared-model",
                "capabilities": {},
                "placements": [
                    placement(STAMP_A, "stamp-a", "us-east"),
                    placement(STAMP_B, "stamp-b", "eu-west"),
                ],
            },
            {
                "account_id": str(ACCOUNT_A),
                "deployment_id": str(DEP_ONLY_A),
                "name": "only-a",
                "model_alias": "a-model",
                "capabilities": {},
                "placements": [placement(STAMP_A, "stamp-a", "us-east")],
            },
            {
                "account_id": str(ACCOUNT_B),
                "deployment_id": str(DEP_OTHER),
                "name": "other",
                "model_alias": "b-model",
                "capabilities": {},
                "placements": [placement(STAMP_B, "stamp-b", "eu-west")],
            },
            {
                "account_id": str(ACCOUNT_C),
                "deployment_id": str(DEP_C),
                "name": "c",
                "model_alias": "c-model",
                "capabilities": {},
                "placements": [placement(STAMP_B, "stamp-b", "eu-west")],
            },
        ],
    }


class Stamps:
    """Every stamp gateway, by host name."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.down: set[str] = set()
        self.broken_after_send: set[str] = set()
        self.status: dict[str, int] = {}

    def count(self, host: str | None = None) -> int:
        return sum(1 for item in self.seen if host is None or item["host"] == host)

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", request.url.host).split(":")[0]
        stamp = host.split(".")[0]
        if stamp in self.down:
            raise httpx.ConnectError("refused", request=request)
        self.seen.append(
            {
                "host": stamp,
                "url": str(request.url),
                "path": request.url.path,
                "headers": dict(request.headers),
                "body": request.content,
                "extensions": dict(request.extensions),
            }
        )
        if stamp in self.broken_after_send:
            raise httpx.ReadError("connection reset after the request was written", request=request)
        code = self.status.get(stamp, 200)
        if request.url.path.startswith("/v1/runs"):
            return httpx.Response(code, json={"stamp": stamp, "data": [{"id": f"run-{stamp}"}]})
        return httpx.Response(
            code,
            json={"served_by": stamp},
            headers={"connection": "keep-alive, x-secret-hop", "x-secret-hop": "1"},
        )


class ControlPlane:
    def __init__(self, signer: SigningKey) -> None:
        self.signer = signer
        self.offline = False
        self.document = table_document()
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if self.offline:
            raise httpx.ConnectError("control plane down", request=request)
        if request.url.path == "/v1/token":
            body = json.loads(request.content)
            assert body["audience"] == "fabric-control"
            if body["api_key"] != "edge-key":
                return httpx.Response(401, json={})
            return httpx.Response(200, json={"access_token": "cp-token", "expires_in": 900})
        if request.url.path == "/v1/edge/routing-table":
            assert request.headers["authorization"] == "Bearer cp-token"
            if request.headers.get("if-none-match") == self.document["etag"]:
                return httpx.Response(304, headers={"etag": self.document["etag"]})
            return httpx.Response(200, json=self.document, headers={"etag": self.document["etag"]})
        if str(request.url) == JWKS_URL:
            return httpx.Response(200, json={"keys": [self.signer.jwk]})
        return httpx.Response(404)


@pytest.fixture
def signer() -> SigningKey:
    return SigningKey()


@pytest.fixture
def stamps() -> Stamps:
    return Stamps()


@pytest.fixture
def control(signer: SigningKey) -> ControlPlane:
    return ControlPlane(signer)


def build_edge(
    control: ControlPlane, stamps: Stamps, *, tmp_path=None, install: bool = True, **overrides
) -> EdgeGateway:
    settings = EdgeSettings(
        control_plane_url="https://control.fabric.test",
        api_key="edge-key",
        jwt_issuer=ISSUER,
        jwks_url=JWKS_URL,
        table_cache_path=str(tmp_path / "table.json") if tmp_path else None,
        **overrides,
    )
    keys = EdgeKeyCache(
        settings.gateway_settings(),
        min_refetch_seconds=30,
        client=httpx.Client(transport=httpx.MockTransport(control.handler)),
    )
    edge = EdgeGateway(
        settings,
        keys=keys,
        stamp_client=httpx.AsyncClient(transport=httpx.MockTransport(stamps.handler)),
        control_client=httpx.AsyncClient(transport=httpx.MockTransport(control.handler)),
    )
    if not control.offline:
        keys.refresh()
    if install:
        edge.store.install(control.document, source="test", persist=False)
    return edge


def client_for(edge: EdgeGateway) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_edge_app(edge)), base_url="https://edge.test"
    )


def auth(signer: SigningKey, account: uuid.UUID = ACCOUNT_A) -> dict[str, str]:
    return {"Authorization": f"Bearer {signer.issue(account_id=account)}"}


async def chat(client, signer, model, account=ACCOUNT_A, headers=None, **extra):
    return await client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}], **extra},
        headers={**auth(signer, account), **(headers or {})},
    )


# -- routing ------------------------------------------------------------------------


async def test_routes_by_alias_to_the_only_stamp_holding_it(control, stamps, signer) -> None:
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        response = await chat(client, signer, "a-model")
    assert response.status_code == 200, response.text
    assert response.json() == {"served_by": "stamp-a"}
    assert response.headers["x-fabric-edge-stamp"] == "stamp-a"
    # Hop-by-hop headers, including those the stamp named in Connection, are stripped.
    assert "x-secret-hop" not in response.headers
    [seen] = stamps.seen
    assert seen["path"] == "/v1/chat/completions"
    assert seen["url"].startswith("https://stamp-a.stamps.example.com/")
    # The caller's credential is forwarded unchanged; the stamp verifies it again.
    assert seen["headers"]["authorization"].startswith("Bearer ")
    assert json.loads(seen["body"])["model"] == "a-model"


async def test_routes_by_deployment_id(control, stamps, signer) -> None:
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        response = await chat(client, signer, str(DEP_ONLY_A))
    assert response.status_code == 200
    assert stamps.seen[0]["host"] == "stamp-a"


async def test_auto_and_cascade_route_to_any_stamp_holding_the_accounts_deployments(
    control, stamps, signer
) -> None:
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        for model in ("auto", "cascade"):
            for _ in range(20):
                response = await chat(client, signer, model)
                assert response.status_code == 200
    assert {item["host"] for item in stamps.seen} == {"stamp-a", "stamp-b"}
    # Account C's only deployment is on stamp B, so its auto requests go nowhere else.
    stamps.seen.clear()
    async with client_for(edge) as client:
        for _ in range(5):
            assert (await chat(client, signer, "auto", account=ACCOUNT_C)).status_code == 200
    assert {item["host"] for item in stamps.seen} == {"stamp-b"}


async def test_another_accounts_model_is_never_a_candidate(control, stamps, signer) -> None:
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        by_alias = await chat(client, signer, "b-model", account=ACCOUNT_A)
        by_id = await chat(client, signer, str(DEP_OTHER), account=ACCOUNT_A)
        unknown = await chat(client, signer, "nope", account=ACCOUNT_A)
        nothing = await chat(client, signer, "auto", account=uuid.uuid4())
    for response in (by_alias, by_id, unknown, nothing):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "model_not_found"
    assert stamps.count() == 0


async def test_a_bad_token_is_refused_before_any_stamp_is_contacted(
    control, stamps, signer
) -> None:
    edge = build_edge(control, stamps)
    other = SigningKey()
    async with client_for(edge) as client:
        missing = await client.post("/v1/chat/completions", json={"model": "a-model"})
        forged = await client.post(
            "/v1/chat/completions",
            json={"model": "a-model"},
            headers={"Authorization": "Bearer not-a-token"},
        )
        wrong_key = await client.post(
            "/v1/chat/completions",
            json={"model": "a-model"},
            headers={"Authorization": f"Bearer {other.issue()}"},
        )
        control_audience = await client.post(
            "/v1/chat/completions",
            json={"model": "a-model"},
            headers={"Authorization": f"Bearer {signer.issue(audience='fabric-control')}"},
        )
        models = await client.get("/v1/models", headers={"Authorization": "Bearer junk"})
    for response in (missing, forged, wrong_key, control_audience, models):
        assert response.status_code == 401, response.text
    assert stamps.count() == 0


async def test_models_is_the_union_for_the_callers_account_only(control, stamps, signer) -> None:
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        response = await client.get("/v1/models", headers=auth(signer))
    assert [row["id"] for row in response.json()["data"]] == ["a-model", "shared-model"]
    assert stamps.count() == 0


async def test_region_preference_and_readiness(control, stamps, signer) -> None:
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        for _ in range(5):
            await chat(client, signer, "shared-model", headers={"X-Fabric-Region": "eu-west"})
    assert {item["host"] for item in stamps.seen} == {"stamp-b"}

    # A ready placement beats a preferred region that is not ready.
    control.document["deployments"][0]["placements"][1]["ready"] = False
    edge.store.install(control.document, source="test", persist=False)
    stamps.seen.clear()
    async with client_for(edge) as client:
        for _ in range(5):
            await chat(client, signer, "shared-model", headers={"X-Fabric-Region": "eu-west"})
    assert {item["host"] for item in stamps.seen} == {"stamp-a"}


async def test_default_region_is_used_without_a_header(control, stamps, signer) -> None:
    edge = build_edge(control, stamps, default_region="us-east")
    async with client_for(edge) as client:
        for _ in range(5):
            await chat(client, signer, "shared-model")
    assert {item["host"] for item in stamps.seen} == {"stamp-a"}


# -- failover and retry rules -------------------------------------------------------


async def test_fails_over_on_a_connection_failure_and_ejects(control, stamps, signer) -> None:
    edge = build_edge(control, stamps, stamp_failure_threshold=1, default_region="us-east")
    stamps.down.add("stamp-a")
    async with client_for(edge) as client:
        response = await chat(client, signer, "shared-model")
        assert response.status_code == 200
        assert response.headers["x-fabric-edge-stamp"] == "stamp-b"
        # Ejected now: the next request goes straight to B despite the region preference.
        stamps.down.clear()
        await chat(client, signer, "shared-model")
    assert [item["host"] for item in stamps.seen] == ["stamp-b", "stamp-b"]
    assert edge.health.snapshot()[STAMP_A]["ejected"] is True

    admin = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_edge_admin_app(edge)), base_url="http://a"
    )
    async with admin:
        metrics = (await admin.get("/metrics")).text
        routing = (await admin.get("/admin/routing")).json()
    assert 'fabric_edge_stamp_ejections_total{stamp="stamp-a"} 1' in metrics
    assert 'fabric_edge_requests_total{stamp="stamp-a",outcome="connect_error"} 1' in metrics
    assert 'fabric_edge_retries_total{stamp="stamp-a"} 1' in metrics
    assert "fabric_edge_routing_table_age_seconds" in metrics
    names = {stamp["name"]: stamp for stamp in routing["table"]["stamps"]}
    assert names["stamp-a"]["health"]["ejected"] is True
    # The summary carries no account ids or model names.
    text = json.dumps(routing)
    assert str(ACCOUNT_A) not in text and "shared-model" not in text


async def test_no_retry_once_the_request_may_have_reached_a_stamp(
    control, stamps, signer
) -> None:
    edge = build_edge(control, stamps, default_region="us-east")
    stamps.broken_after_send.add("stamp-a")
    async with client_for(edge) as client:
        response = await chat(client, signer, "shared-model")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "stamp_unavailable"
    assert [item["host"] for item in stamps.seen] == ["stamp-a"]


async def test_every_candidate_down_is_a_503(control, stamps, signer) -> None:
    edge = build_edge(control, stamps)
    stamps.down.update({"stamp-a", "stamp-b"})
    async with client_for(edge) as client:
        response = await chat(client, signer, "shared-model")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_stamp_reachable"


async def test_stamp_errors_are_passed_through_not_retried(control, stamps, signer) -> None:
    edge = build_edge(control, stamps, default_region="us-east")
    stamps.status["stamp-a"] = 429
    async with client_for(edge) as client:
        response = await chat(client, signer, "shared-model")
    assert response.status_code == 429
    assert stamps.count() == 1


# -- affinity -----------------------------------------------------------------------


async def test_a_run_id_sticks_to_one_stamp(control, stamps, signer) -> None:
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        for run in range(12):
            stamps.seen.clear()
            for _ in range(4):
                await chat(client, signer, "shared-model", headers={"X-Fabric-Run-Id": f"r{run}"})
            assert len({item["host"] for item in stamps.seen}) == 1


async def test_run_ids_spread_across_stamps(control, stamps, signer) -> None:
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        for run in range(30):
            await chat(client, signer, "shared-model", headers={"X-Fabric-Run-Id": f"spread-{run}"})
    assert {item["host"] for item in stamps.seen} == {"stamp-a", "stamp-b"}


async def test_a_run_never_moves_to_another_stamp(control, stamps, signer) -> None:
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        await chat(client, signer, "shared-model", headers={"X-Fabric-Run-Id": "pinned"})
        home = stamps.seen[0]["host"]
        stamps.down.add(home)
        stamps.seen.clear()
        response = await chat(client, signer, "shared-model", headers={"X-Fabric-Run-Id": "pinned"})
    assert response.status_code == 503
    assert stamps.seen == []


async def test_a_session_key_is_sticky_but_soft(control, stamps, signer) -> None:
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        for _ in range(4):
            await chat(client, signer, "shared-model", headers={"X-Fabric-Session": "s1"})
        homes = {item["host"] for item in stamps.seen}
        assert len(homes) == 1
        stamps.down.add(homes.pop())
        response = await chat(client, signer, "shared-model", headers={"X-Fabric-Session": "s1"})
    assert response.status_code == 200


async def test_run_lookup_finds_the_stamp_holding_the_run(control, stamps, signer) -> None:
    edge = build_edge(control, stamps)
    stamps.status["stamp-a"] = 404
    async with client_for(edge) as client:
        response = await client.get("/v1/runs/run-1", headers=auth(signer))
        listed = await client.get("/v1/runs", headers=auth(signer))
    stamps.status.clear()
    assert response.status_code == 200
    assert response.headers["x-fabric-edge-stamp"] == "stamp-b"
    assert listed.status_code == 200
    # Gathered from every stamp of the account that answered.
    assert {item["stamp"] for item in listed.json()["data"]} == {"stamp-b"}


# -- streaming ----------------------------------------------------------------------


async def test_streams_without_buffering(control, signer) -> None:
    """The first event reaches the caller before the stamp has produced the second."""
    release = asyncio.Event()

    async def events():
        yield b'data: {"delta":"first"}\n\n'
        await release.wait()
        yield b"data: [DONE]\n\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=events()
        )

    stamps = Stamps()
    edge = build_edge(control, stamps)
    edge.stamp_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    from starlette.requests import Request

    from fabric_data_plane.edge.app import forward

    body = json.dumps({"model": "shared-model", "stream": True}).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"host", b"edge.test"), (b"authorization", b"Bearer x")],
        "scheme": "https",
        "client": ("203.0.113.9", 1234),
        "server": ("edge.test", 443),
    }
    request = Request(scope)
    principal = edge.authenticate(auth(signer)["Authorization"])
    table = edge.store.table
    response = await forward(
        edge,
        request,
        principal=principal,
        candidates=table.candidates(ACCOUNT_A, "shared-model"),
        body=body,
        started=0.0,
    )
    assert response.headers["content-type"] == "text/event-stream"
    iterator = response.body_iterator.__aiter__()
    first = await asyncio.wait_for(iterator.__anext__(), timeout=2)
    assert first == b'data: {"delta":"first"}\n\n'
    release.set()
    rest = [chunk async for chunk in iterator]
    assert rest == [b"data: [DONE]\n\n"]
    assert edge.health.in_flight(table.candidates(ACCOUNT_A, "shared-model")[0].stamp.stamp_id) == 0


async def test_sse_passes_through_the_asgi_listener(control, signer) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"data: a\n\ndata: b\n\ndata: [DONE]\n\n",
        )

    edge = build_edge(control, Stamps())
    edge.stamp_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with client_for(edge) as client:
        response = await chat(client, signer, "a-model", stream=True)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == "data: a\n\ndata: b\n\ndata: [DONE]\n\n"


# -- other surfaces -----------------------------------------------------------------


async def test_messages_accepts_x_api_key_and_answers_errors_in_anthropic_shape(
    control, stamps, signer
) -> None:
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        ok = await client.post(
            "/v1/messages",
            json={"model": "a-model", "max_tokens": 5, "messages": []},
            headers={"x-api-key": signer.issue(), "anthropic-version": "2023-06-01"},
        )
        refused = await client.post(
            "/v1/messages", json={"model": "a-model"}, headers={"x-api-key": "junk"}
        )
    assert ok.status_code == 200
    assert stamps.seen[0]["path"] == "/v1/messages"
    assert stamps.seen[0]["headers"]["x-api-key"]
    assert refused.status_code == 401
    assert refused.json()["type"] == "error"


async def test_audio_routes_by_the_multipart_model_field(control, stamps, signer) -> None:
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        response = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.wav", b"RIFF....", "audio/wav")},
            data={"model": "a-model"},
            headers=auth(signer),
        )
    assert response.status_code == 200, response.text
    assert stamps.seen[0]["path"] == "/v1/audio/transcriptions"
    assert b'name="model"' in stamps.seen[0]["body"]


async def test_agent_runs_route_is_forwarded(control, stamps, signer) -> None:
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        response = await client.post(
            "/v1/agent/runs", json={"model": "a-model", "input": "x"}, headers=auth(signer)
        )
    assert response.status_code == 200
    assert stamps.seen[0]["path"] == "/v1/agent/runs"


async def test_oversized_and_malformed_bodies_never_reach_a_stamp(
    control, stamps, signer
) -> None:
    edge = build_edge(control, stamps, max_json_body_bytes=2048)
    deep: Any = []
    for _ in range(200):
        deep = [deep]
    async with client_for(edge) as client:
        large = await client.post(
            "/v1/chat/completions",
            content=json.dumps({"model": "a-model", "x": "y" * 5000}),
            headers={**auth(signer), "content-type": "application/json"},
        )
        bad = await client.post(
            "/v1/chat/completions", content=b"{nope", headers=auth(signer)
        )
        nested = await client.post(
            "/v1/chat/completions",
            content=json.dumps({"model": "a-model", "x": deep}),
            headers=auth(signer),
        )
    assert large.status_code == 413
    assert bad.status_code == 400
    assert nested.status_code == 400
    assert stamps.count() == 0


async def test_address_overrides_keep_the_reported_name_for_tls(control, stamps, signer) -> None:
    edge = build_edge(
        control,
        stamps,
        stamp_address_overrides={"stamp-a.stamps.example.com:443": "127.0.0.1:9443"},
    )
    async with client_for(edge) as client:
        await chat(client, signer, "a-model")
    [seen] = stamps.seen
    assert seen["url"].startswith("https://127.0.0.1:9443/v1/chat/completions")
    assert seen["headers"]["host"] == "stamp-a.stamps.example.com"
    assert seen["extensions"]["sni_hostname"] == "stamp-a.stamps.example.com"


# -- routing table lifecycle --------------------------------------------------------


async def test_polls_persists_and_serves_through_a_control_plane_outage(
    control, stamps, signer, tmp_path
) -> None:
    edge = build_edge(control, stamps, tmp_path=tmp_path, install=False)
    assert edge.readiness()["ready"] is False
    assert await edge.poller.poll_once() == "updated"
    assert edge.readiness()["ready"] is True
    assert (tmp_path / "table.json").stat().st_mode & 0o777 == 0o600
    # Unchanged: a conditional read.
    assert await edge.poller.poll_once() == "not_modified"

    # The control plane goes away; the running edge keeps serving.
    control.offline = True
    assert await edge.poller.poll_once() == "error"
    async with client_for(edge) as client:
        assert (await chat(client, signer, "a-model")).status_code == 200

    # And a fresh edge started during the outage serves from disk.
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps({"keys": [signer.jwk]}))
    restarted = build_edge(control, stamps, tmp_path=tmp_path, install=False, jwks_file=str(jwks))
    assert restarted.store.source == "disk"
    assert restarted.readiness()["ready"] is True
    calls = len(control.calls)
    async with client_for(restarted) as client:
        response = await chat(client, signer, "a-model")
    assert response.status_code == 200
    assert response.headers["x-fabric-edge-stamp"] == "stamp-a"
    assert len(control.calls) == calls


async def test_inference_never_contacts_the_control_plane(control, stamps, signer) -> None:
    edge = build_edge(control, stamps)
    before = list(control.calls)
    async with client_for(edge) as client:
        for model in ("a-model", "shared-model", "auto"):
            await chat(client, signer, model)
        await client.get("/v1/models", headers=auth(signer))
    assert control.calls == before


async def test_an_older_revision_is_refused(control, stamps) -> None:
    edge = build_edge(control, stamps, install=False)
    await edge.poller.poll_once()
    control.document = table_document(revision=5, etag='"older"')
    control.document["deployments"] = []
    assert await edge.poller.poll_once() == "stale_revision"
    assert edge.store.table.revision == 10
    assert edge.store.table.deployments_for(ACCOUNT_A)


async def test_readiness_needs_keys_as_well_as_a_table(control, stamps) -> None:
    control.offline = True
    edge = build_edge(control, stamps)
    assert edge.readiness()["routing_table"] is True
    assert edge.readiness()["ready"] is False
    async with client_for(edge) as client:
        assert (await client.get("/readyz")).status_code == 503


def test_gateway_origin_accepts_only_https_bases() -> None:
    assert gateway_origin("https://s.example.com/v1") == "https://s.example.com"
    assert gateway_origin("https://s.example.com:8443") == "https://s.example.com:8443"
    assert gateway_origin("http://s.example.com") is None
    assert gateway_origin("https://user:pw@s.example.com") is None
    assert gateway_origin("https://s.example.com/other") is None


def test_malformed_entries_are_skipped() -> None:
    document = table_document()
    document["deployments"].append({"account_id": "nope"})
    document["deployments"][0]["placements"].append(
        {**placement(STAMP_A, "x", "r"), "inference_url": "http://plain.example.com"}
    )
    table = RoutingTable(document)
    assert len(table.candidates(ACCOUNT_A, "shared-model")) == 2
