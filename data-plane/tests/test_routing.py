"""Inference ingress behaviour: routing, isolation, streaming, and usage."""

from __future__ import annotations

import pytest

from tests.conftest import (
    ACCOUNT_A,
    ACCOUNT_B,
    DEPLOYMENT_A,
    ControlPlaneStub,
    SigningKey,
    UpstreamStub,
    make_settings,
)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def chat(model: str = "launch-model", **extra) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        **extra,
    }


async def test_chat_completion_is_proxied_to_the_deployment(
    client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    response = await client.post(
        "/v1/chat/completions", json=chat(), headers=auth(signing_key.issue())
    )
    assert response.status_code == 200, response.text

    body = response.json()
    # The customer sees their alias, not the internal release name.
    assert body["model"] == "launch-model"
    assert body["choices"][0]["message"]["content"] == "hi"

    assert len(upstream.requests) == 1
    forwarded = upstream.requests[0]
    assert forwarded["url"] == "http://model-host.test/v1/chat/completions"
    # The upstream is addressed by its own model name.
    assert forwarded["payload"]["model"] == "internal-release-1"
    assert "authorization" not in forwarded["headers"]


async def test_missing_credentials_are_rejected(client) -> None:
    response = await client.post("/v1/chat/completions", json=chat())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_credentials"


async def test_another_accounts_model_is_refused(
    client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    """Naming a deployment owned by another account must not serve it."""
    response = await client.post(
        "/v1/chat/completions",
        json=chat("other-account-model"),
        headers=auth(signing_key.issue()),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "model_not_available"
    # Nothing reached the model host.
    assert upstream.requests == []


async def test_unknown_model_is_not_found(client, signing_key: SigningKey) -> None:
    response = await client.post(
        "/v1/chat/completions", json=chat("no-such-model"), headers=auth(signing_key.issue())
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


async def test_deployment_id_also_routes(client, signing_key: SigningKey) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json=chat(str(DEPLOYMENT_A)),
        headers=auth(signing_key.issue()),
    )
    assert response.status_code == 200


async def test_model_is_required(client, signing_key: SigningKey) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={"messages": []},
        headers=auth(signing_key.issue()),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_required"


async def test_invalid_json_is_rejected(client, signing_key: SigningKey) -> None:
    response = await client.post(
        "/v1/chat/completions",
        content=b"not json",
        headers={**auth(signing_key.issue()), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


async def test_model_listing_is_scoped_to_the_account(
    client, signing_key: SigningKey
) -> None:
    own = await client.get("/v1/models", headers=auth(signing_key.issue()))
    assert own.status_code == 200
    assert [entry["id"] for entry in own.json()["data"]] == ["launch-model"]

    other = await client.get("/v1/models", headers=auth(signing_key.issue(account_id=ACCOUNT_B)))
    assert [entry["id"] for entry in other.json()["data"]] == ["other-account-model"]


async def test_streaming_is_passed_through(
    client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json=chat(stream=True),
        headers=auth(signing_key.issue()),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert b"delta" in response.content
    assert upstream.requests[0]["payload"]["stream"] is True


async def test_upstream_failure_surfaces_as_unavailable(
    client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    upstream.fail = True
    response = await client.post(
        "/v1/chat/completions", json=chat(), headers=auth(signing_key.issue())
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "upstream_unavailable"


async def test_upstream_error_status_is_relayed(
    client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    upstream.status = 429
    response = await client.post(
        "/v1/chat/completions", json=chat(), headers=auth(signing_key.issue())
    )
    assert response.status_code == 429


async def test_inference_does_not_call_the_control_plane(
    client, signing_key: SigningKey, control_plane: ControlPlaneStub, plane
) -> None:
    """AR-CP02/AR-DP02: the request path must not depend on the control plane."""
    # Warm the key cache, then take the control plane away entirely.
    await client.post("/v1/chat/completions", json=chat(), headers=auth(signing_key.issue()))
    fetches_after_warm = control_plane.fetches
    control_plane.offline = True

    for _ in range(5):
        response = await client.post(
            "/v1/chat/completions", json=chat(), headers=auth(signing_key.issue())
        )
        assert response.status_code == 200

    assert control_plane.fetches == fetches_after_warm


async def test_usage_is_buffered_with_verified_ownership(
    client, signing_key: SigningKey, plane
) -> None:
    await client.post("/v1/chat/completions", json=chat(), headers=auth(signing_key.issue()))

    records = plane.usage.drain()
    assert len(records) == 1
    record = records[0]
    assert record.deployment_id == DEPLOYMENT_A
    assert record.input_tokens == 11
    assert record.output_tokens == 7
    assert record.streamed is False


async def test_refused_requests_are_not_recorded_as_usage(
    client, signing_key: SigningKey, plane
) -> None:
    await client.post(
        "/v1/chat/completions",
        json=chat("other-account-model"),
        headers=auth(signing_key.issue()),
    )
    assert plane.usage.drain() == []


async def test_usage_buffer_is_bounded_and_reports_drops(plane) -> None:
    import datetime as dt

    from fabric_data_plane.usage import UsageBuffer, UsageRecord

    buffer = UsageBuffer(capacity=2)
    for _ in range(5):
        buffer.record(
            UsageRecord(
                account_id=DEPLOYMENT_A,
                deployment_id=DEPLOYMENT_A,
                input_tokens=1,
                output_tokens=1,
                streamed=False,
                occurred_at=dt.datetime.now(tz=dt.UTC),
            )
        )
    state = buffer.snapshot()
    assert state["buffered"] == 2
    assert state["recorded"] == 5
    assert state["dropped"] == 3
    # The data plane is drained, never pushing, so a growing buffer means no
    # collector is running rather than a broken exporter.
    assert state["export_mode"] == "drain"


async def test_admin_listener_carries_no_inference_routes(admin_app, inference_app) -> None:
    """AR-DP03: administrative and inference listeners are separate apps."""
    admin_paths = {route.path for route in admin_app.routes if hasattr(route, "path")}
    inference_paths = {route.path for route in inference_app.routes if hasattr(route, "path")}

    assert "/v1/chat/completions" in inference_paths
    assert "/v1/chat/completions" not in admin_paths
    # Administrative endpoints never appear on the public listener. This is the
    # direction that matters: the drain is destructive and the state endpoints
    # describe internals.
    assert not {path for path in inference_paths if path.startswith("/admin")}
    assert "/admin/keys" in admin_paths
    # Probes are deliberately on both. The administrative listener binds to
    # localhost, so a kubelet probing the pod address can only reach the public one.
    assert "/healthz" in admin_paths and "/healthz" in inference_paths


async def test_admin_reports_readiness_and_state(admin_client, plane, signing_key) -> None:
    # Readiness is false until key material is held, since no token could be verified.
    assert (await admin_client.get("/healthz")).json() == {"status": "ok"}

    plane.keys.key_for(signing_key.kid)  # warm the cache
    ready = await admin_client.get("/readyz")
    assert ready.json()["status"] == "ready"
    assert ready.json()["deployments"] == 2

    keys = await admin_client.get("/admin/keys")
    assert keys.json()["keys_held"] == 1
    assert (await admin_client.get("/admin/usage")).json()["buffered"] == 0


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/completions"])
async def test_both_openai_paths_are_served(
    client, signing_key: SigningKey, upstream: UpstreamStub, path: str
) -> None:
    response = await client.post(path, json=chat(), headers=auth(signing_key.issue()))
    assert response.status_code == 200
    assert upstream.requests[-1]["url"].endswith(path)


async def test_draining_usage_returns_records_and_empties_the_buffer(
    client, admin_client, signing_key: SigningKey
) -> None:
    """A collector takes records once; a second drain must not replay them."""
    reply = await client.post(
        "/v1/chat/completions", json=chat(), headers=auth(signing_key.issue())
    )
    assert reply.status_code == 200, reply.text

    first = await admin_client.post("/admin/usage/drain")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["count"] == 1
    record = body["records"][0]
    # Ownership on an exported record comes from the verified token and the local
    # registry, so a collector never decides who is billed.
    assert record["account_id"] == str(ACCOUNT_A)
    assert record["deployment_id"] == str(DEPLOYMENT_A)
    assert record["record_id"]

    second = await admin_client.post("/admin/usage/drain")
    assert second.json() == {"records": [], "count": 0}


async def test_each_call_is_independently_deduplicable(
    client, admin_client, signing_key: SigningKey
) -> None:
    for _ in range(2):
        await client.post("/v1/chat/completions", json=chat(), headers=auth(signing_key.issue()))

    records = (await admin_client.post("/admin/usage/drain")).json()["records"]
    assert len({record["record_id"] for record in records}) == 2


async def test_the_inference_listener_cannot_drain_usage(
    client, signing_key: SigningKey
) -> None:
    """Draining is administrative: a caller on the inference path cannot take usage."""
    response = await client.post("/admin/usage/drain", headers=auth(signing_key.issue()))
    assert response.status_code == 404


async def test_both_listeners_share_one_usage_buffer(tmp_path, monkeypatch) -> None:
    """The drained buffer must be the buffer that served the request.

    Running the administrative listener in its own process would drain a buffer
    that never saw any traffic and report no usage, silently. This asserts the
    deployment entrypoint builds one plane for both applications.
    """
    from fabric_data_plane import serve
    from fabric_data_plane.app import create_admin_app, create_inference_app

    built: list = []
    real_build = serve.build_plane

    def record(settings=None):
        plane = real_build(settings)
        built.append(plane)
        return plane

    monkeypatch.setattr(serve, "build_plane", record)

    # Stand in for uvicorn so no socket is opened by the test.
    class DummyServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        async def serve(self):
            return None

    monkeypatch.setattr(serve.uvicorn, "Server", DummyServer)
    monkeypatch.setattr(serve.uvicorn, "Config", lambda app, **kwargs: app)

    settings = make_settings(deployments_file=str(tmp_path / "deployments.json"))
    (tmp_path / "deployments.json").write_text('{"deployments": []}')

    await serve.serve(settings)

    assert len(built) == 1, "each listener built its own plane, so usage would be lost"
    inference_app = create_inference_app(built[0])
    admin_app = create_admin_app(built[0])
    assert inference_app is not admin_app


async def test_inference_listener_exposes_probes(client, signing_key: SigningKey) -> None:
    """A kubelet cannot reach the localhost-bound administrative listener."""
    alive = await client.get("/healthz")
    assert alive.status_code == 200
    assert alive.json() == {"status": "ok"}

    # Keys are fetched on first use, so a data plane that has never served a request
    # is legitimately not ready yet.
    assert (await client.get("/readyz")).status_code == 503

    served = await client.post(
        "/v1/chat/completions", json=chat(), headers=auth(signing_key.issue())
    )
    assert served.status_code == 200, served.text

    ready = await client.get("/readyz")
    assert ready.status_code == 200
    # Only a status: key and deployment counts stay on the administrative listener.
    assert ready.json() == {"status": "ready"}


async def test_readiness_fails_with_a_status_code_when_no_keys_are_held(
    plane, client, admin_client
) -> None:
    """A probe reads the status code, not the body.

    Returning 200 with "unavailable" would let Kubernetes send traffic to a data
    plane that rejects every request.
    """
    plane.keys._keys.clear()

    public = await client.get("/readyz")
    assert public.status_code == 503
    assert public.json() == {"status": "unavailable"}

    admin = await admin_client.get("/readyz")
    assert admin.status_code == 503
    assert admin.json()["status"] == "unavailable"
