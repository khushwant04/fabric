"""Opt-in routing diagnostics preserve protocol, metering, and tenant boundaries."""

from __future__ import annotations

import uuid
from urllib.parse import quote

import pytest

from fabric_data_plane.registry import Deployment, DeploymentRegistry, ModelCapabilities
from tests.conftest import ACCOUNT_A, ACCOUNT_B, DEPLOYMENT_A, DEPLOYMENT_B


def _diagnostics(response) -> dict[str, str]:
    return {name: value for name, value in response.headers.items() if name.startswith("x-fabric-")}


def _payload(path: str, model: str, text: str, **extra) -> dict:
    content = (
        {"messages": [{"role": "user", "content": text}]}
        if path == "/v1/chat/completions"
        else {"prompt": text}
    )
    return {"model": model, **content, **extra}


@pytest.fixture
def diagnostic_plane(plane):
    deployments = [
        Deployment(
            deployment_id=DEPLOYMENT_A,
            account_id=ACCOUNT_A,
            model_alias="general-model",
            upstream_url="http://general-private.test",
            upstream_model="private-general-release",
        ),
        Deployment(
            deployment_id=uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
            account_id=ACCOUNT_A,
            model_alias="code-model",
            upstream_url="http://code-private.test",
            upstream_model="private-code-release",
            capabilities=ModelCapabilities(code=True),
        ),
        Deployment(
            deployment_id=uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
            account_id=ACCOUNT_A,
            model_alias="reasoning-model",
            upstream_url="http://reasoning-private.test",
            capabilities=ModelCapabilities(reasoning=True),
        ),
        Deployment(
            deployment_id=uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
            account_id=ACCOUNT_A,
            model_alias="vision-model",
            upstream_url="http://vision-private.test",
            capabilities=ModelCapabilities(vision=True),
        ),
        Deployment(
            deployment_id=DEPLOYMENT_B,
            account_id=ACCOUNT_B,
            model_alias="other-account-model",
            upstream_url="http://other-private.test",
            capabilities=ModelCapabilities(code=True, priority=100),
        ),
    ]
    plane.registry = DeploymentRegistry(deployments)
    return plane


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/completions"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("text", "selected", "task", "reason"),
    [
        ("hello", "general-model", "general", "auto_general_fit"),
        ("write a Python function", "code-model", "code", "auto_task_fit"),
        ("solve this equation", "reasoning-model", "reasoning", "auto_task_fit"),
    ],
)
async def test_auto_diagnostics_preserve_response_and_usage(
    client, diagnostic_plane, signing_key, upstream, path, stream, text, selected, task, reason
) -> None:
    headers = {"Authorization": f"Bearer {signing_key.issue()}"}
    baseline = await client.post(
        path, json=_payload(path, "auto", text, stream=stream), headers=headers
    )
    assert baseline.status_code == 200
    assert _diagnostics(baseline) == {}
    diagnostic_plane.usage.drain()

    response = await client.post(
        path,
        json=_payload(path, "auto", text, stream=stream, routing={"explain": True}),
        headers=headers,
    )

    assert response.status_code == baseline.status_code
    assert response.content == baseline.content
    assert response.headers["content-type"] == baseline.headers["content-type"]
    assert _diagnostics(response) == {
        "x-fabric-selected-model": selected,
        "x-fabric-routing-task": task,
        "x-fabric-routing-reason": reason,
        "x-fabric-routing-policy": "heuristic-v1",
    }
    assert "routing" not in upstream.requests[-1]["payload"]
    values = " ".join(_diagnostics(response).values())
    assert "private" not in values and str(ACCOUNT_A) not in values
    records = diagnostic_plane.usage.drain()
    assert len(records) == 1
    assert (records[0].input_tokens, records[0].output_tokens) == (11, 7)
    assert records[0].streamed is stream
    assert not diagnostic_plane._admitted_streams
    assert diagnostic_plane.concurrency.snapshot()["in_flight"] == 0


async def test_vision_selection_explains_capability_fit(
    client, diagnostic_plane, signing_key, upstream
) -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {"type": "image_url", "image_url": {"url": "https://image.test/private.jpg"}},
            ],
        }
    ]
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": messages, "routing": {"explain": True}},
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert response.status_code == 200
    assert response.headers["x-fabric-selected-model"] == "vision-model"
    assert response.headers["x-fabric-routing-reason"] == "auto_capability_fit"
    assert upstream.requests[-1]["payload"]["messages"] == messages
    assert "private.jpg" not in " ".join(_diagnostics(response).values())


@pytest.mark.parametrize("explain", [False, True])
async def test_vision_requirement_survives_text_inspection_limit(
    client, diagnostic_plane, signing_key, upstream, explain
) -> None:
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "a" * 200_001},
        {"type": "image_url", "image_url": {"url": "https://image.test/private.jpg"}},
    ]}]
    payload = {"model": "auto", "messages": messages}
    if explain:
        payload["routing"] = {"explain": True}
    response = await client.post(
        "/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert response.status_code == 200
    assert upstream.requests[-1]["url"] == "http://vision-private.test/v1/chat/completions"
    if explain:
        assert response.headers["x-fabric-selected-model"] == "vision-model"


async def test_image_request_never_auto_routes_to_text_only_host(
    client, plane, signing_key, upstream
) -> None:
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "a" * 200_001},
        {"type": "image_url", "image_url": {"url": "https://image.test/private.jpg"}},
    ]}]
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": messages},
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "auto_model_not_found"
    assert not upstream.requests
    assert not plane.usage.drain()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("model", ["general-model", str(DEPLOYMENT_A)])
async def test_exact_requests_stay_pinned_and_consume_routing(
    client, diagnostic_plane, signing_key, upstream, model, stream
) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json=_payload(
            "/v1/chat/completions",
            model,
            "hello",
            stream=stream,
            routing={"task": "code", "explain": True},
        ),
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert response.status_code == 200
    assert response.headers["x-fabric-selected-model"] == "general-model"
    assert response.headers["x-fabric-routing-task"] == "code"
    assert response.headers["x-fabric-routing-reason"] == "exact_model_pinned"
    assert upstream.requests[-1]["payload"]["model"] == "private-general-release"
    assert "routing" not in upstream.requests[-1]["payload"]


async def test_concrete_auto_alias_retains_exact_pinning(
    client, diagnostic_plane, signing_key, upstream
) -> None:
    diagnostic_plane.registry = DeploymentRegistry(
        [
            Deployment(
                deployment_id=DEPLOYMENT_A,
                account_id=ACCOUNT_A,
                model_alias="auto",
                upstream_url="http://private-auto.test",
                upstream_model="private-auto-release",
            ),
        ]
    )
    response = await client.post(
        "/v1/chat/completions",
        json=_payload(
            "/v1/chat/completions",
            "auto",
            "write code",
            routing={"explain": True},
        ),
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert response.status_code == 200
    assert response.headers["x-fabric-routing-reason"] == "exact_model_pinned"
    assert upstream.requests[-1]["payload"]["model"] == "private-auto-release"
    assert "routing" not in upstream.requests[-1]["payload"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("model", ["auto", "general-model"])
@pytest.mark.parametrize("routing", [{}, {"task": "code"}, {"explain": False}])
async def test_diagnostics_are_absent_without_explicit_opt_in(
    client, diagnostic_plane, signing_key, upstream, model, routing, stream
) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json=_payload("/v1/chat/completions", model, "hello", stream=stream, routing=routing),
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert response.status_code == 200
    assert _diagnostics(response) == {}
    assert "routing" not in upstream.requests[-1]["payload"]


@pytest.mark.parametrize("model", ["auto", "general-model"])
@pytest.mark.parametrize(
    "routing",
    [
        None,
        [],
        "explain",
        {"explain": None},
        {"explain": "true"},
        {"explain": 1},
        {"explain": []},
        {"explain": {}},
        {"task": None},
        {"task": "invalid"},
        {"explain": True, "quality": "best"},
    ],
)
async def test_malformed_extensions_are_rejected_before_model_work(
    client, diagnostic_plane, signing_key, upstream, model, routing
) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json=_payload("/v1/chat/completions", model, "hello", routing=routing),
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_routing"
    assert upstream.requests == []
    assert _diagnostics(response) == {}
    assert diagnostic_plane.usage.drain() == []


async def test_diagnostics_are_scoped_to_authenticated_account(
    client, diagnostic_plane, signing_key, upstream
) -> None:
    payload = _payload(
        "/v1/chat/completions", "auto", "write a Python function", routing={"explain": True}
    )
    own = await client.post(
        "/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    other = await client.post(
        "/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {signing_key.issue(account_id=ACCOUNT_B)}"},
    )
    assert own.headers["x-fabric-selected-model"] == "code-model"
    assert other.headers["x-fabric-selected-model"] == "other-account-model"
    attempted = len(upstream.requests)
    forbidden = await client.post(
        "/v1/chat/completions",
        json={**payload, "model": "other-account-model"},
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert forbidden.status_code == 403
    assert _diagnostics(forbidden) == {}
    unauthenticated = await client.post("/v1/chat/completions", json=payload)
    assert unauthenticated.status_code == 401
    assert _diagnostics(unauthenticated) == {}
    assert len(upstream.requests) == attempted


@pytest.mark.parametrize("stream", [False, True])
async def test_diagnostics_preserve_upstream_error_behavior(
    client, diagnostic_plane, signing_key, upstream, stream
) -> None:
    upstream.status = 429
    headers = {"Authorization": f"Bearer {signing_key.issue()}"}
    baseline = await client.post(
        "/v1/chat/completions",
        json=_payload("/v1/chat/completions", "auto", "hello", stream=stream),
        headers=headers,
    )
    response = await client.post(
        "/v1/chat/completions",
        json=_payload(
            "/v1/chat/completions", "auto", "hello", stream=stream, routing={"explain": True}
        ),
        headers=headers,
    )
    assert response.status_code == baseline.status_code
    assert response.content == baseline.content
    assert response.headers["x-fabric-selected-model"] == "general-model"
    assert diagnostic_plane.usage.drain() == []


async def test_header_alias_is_bounded_and_encoded_without_changing_response_body(
    client, diagnostic_plane, signing_key
) -> None:
    alias = "モデル\r\nPrivate: value" + "界" * 250
    diagnostic_plane.registry = DeploymentRegistry(
        [
            Deployment(
                deployment_id=DEPLOYMENT_A,
                account_id=ACCOUNT_A,
                model_alias=alias,
                upstream_url="http://private.test",
            ),
        ]
    )
    response = await client.post(
        "/v1/chat/completions",
        json=_payload(
            "/v1/chat/completions", str(DEPLOYMENT_A), "hello", routing={"explain": True}
        ),
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert response.status_code == 200
    value = response.headers["x-fabric-selected-model"]
    assert value == quote(alias[:200], safe="-._~")
    assert value.isascii() and "\r" not in value and "\n" not in value
    assert len(value) <= 2400
    assert response.json()["model"] == alias
