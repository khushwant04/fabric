"""Data-zone enforcement at the gateway (ADR 0017).

Placement keeps a zone-restricted deployment off out-of-zone stamps, but it decides once.
These tests cover the per-request half: a zone-bound token or a ``routing.data_zone`` hint
is refused by a stamp outside that zone, a deployment whose residency the stamp no longer
satisfies is refused rather than served, automatic selection never picks one, and every
unknown fails closed.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import uuid
from typing import Any

import httpx
import jwt
import pytest

from fabric_data_plane.app import DataPlane, create_inference_app
from fabric_data_plane.auth import verify_inference_token
from fabric_data_plane.errors import Unauthorized
from fabric_data_plane.keys import KeyCache
from fabric_data_plane.registry import DeploymentRegistry, ReloadingRegistry
from fabric_data_plane.residency import (
    DataBoundary,
    Residency,
    boundary_from_payload,
    residency_from_entry,
    residency_refusal,
    zone_refusal,
)
from tests.conftest import (
    ACCOUNT_A,
    ISSUER,
    UPSTREAM,
    ControlPlaneStub,
    SigningKey,
    UpstreamStub,
    make_settings,
)

EU_DEPLOYMENT = uuid.UUID("00000000-0000-4000-8000-0000000000e1")
US_DEPLOYMENT = uuid.UUID("00000000-0000-4000-8000-0000000000e2")
OPEN_DEPLOYMENT = uuid.UUID("00000000-0000-4000-8000-0000000000e3")
EU_BOUNDARY = {"region": "westeurope", "data_zone": "eu"}


def token(signing_key: SigningKey, *, data_zone: Any = None) -> str:
    now = dt.datetime.now(tz=dt.UTC)
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": str(uuid.uuid4()),
        "aud": "fabric-inference",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + dt.timedelta(minutes=15)).timestamp()),
        "jti": uuid.uuid4().hex,
        "account_id": str(ACCOUNT_A),
        "principal_type": "user",
        "scp": ["inference:invoke"],
    }
    if data_zone is not None:
        claims["data_zone"] = data_zone
    return jwt.encode(claims, signing_key.pem, algorithm="RS256", headers={"kid": signing_key.kid})


def auth(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def document(boundary: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "deployments": [
            {
                "deployment_id": str(EU_DEPLOYMENT),
                "account_id": str(ACCOUNT_A),
                "model_alias": "eu-model",
                "upstream_url": UPSTREAM,
                "residency": {"level": "data_zone", "data_zone": "eu"},
                # Highest priority, so automatic selection would pick it when it may.
                "capabilities": {"priority": 50},
            },
            {
                "deployment_id": str(US_DEPLOYMENT),
                "account_id": str(ACCOUNT_A),
                "model_alias": "us-model",
                "upstream_url": UPSTREAM,
                "residency": {"level": "regional", "regions": ["eastus"]},
                "capabilities": {"priority": 100},
            },
            {
                "deployment_id": str(OPEN_DEPLOYMENT),
                "account_id": str(ACCOUNT_A),
                "model_alias": "open-model",
                "upstream_url": UPSTREAM,
            },
        ]
    }
    if boundary is not None:
        payload["data_boundary"] = boundary
    return payload


@pytest.fixture
async def make_client(control_plane: ControlPlaneStub, upstream: UpstreamStub):
    clients: list[httpx.AsyncClient] = []

    async def build(boundary: dict[str, Any] | None) -> tuple[httpx.AsyncClient, DataPlane]:
        settings = make_settings()
        plane = DataPlane(
            settings=settings,
            keys=KeyCache(settings, client=control_plane.client()),
            registry=DeploymentRegistry.from_payload(document(boundary)),
            client=upstream.client(),
        )
        transport = httpx.ASGITransport(app=create_inference_app(plane))
        http = httpx.AsyncClient(transport=transport, base_url="http://ingress.test")
        clients.append(http)
        return http, plane

    yield build
    for http in clients:
        await http.aclose()


def chat(model: str, **extra: Any) -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": "hello"}], **extra}


# --- policy -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, Residency()),
        ({"level": "global"}, Residency()),
        ({"level": "data_zone", "data_zone": "EU"}, Residency(level="data_zone", data_zone="eu")),
        (
            {"level": "regional", "regions": ["WestEurope", "eastus"]},
            Residency(level="regional", regions=("eastus", "westeurope")),
        ),
        # Every unreadable shape is the strictest reading, never global.
        ({"level": "planetary"}, Residency(level="regional")),
        ({"level": "data_zone"}, Residency(level="regional")),
        ({"level": "regional", "regions": "eastus"}, Residency(level="regional")),
        ("eu", Residency(level="regional")),
    ],
)
def test_residency_parsing_fails_closed(raw: Any, expected: Residency) -> None:
    assert residency_from_entry(raw) == expected


def test_refusals() -> None:
    eu = DataBoundary(region="westeurope", data_zone="eu")
    unknown_zone = DataBoundary(region="hq-dc1", data_zone=None)
    zoned = Residency(level="data_zone", data_zone="eu")
    regional = Residency(level="regional", regions=("hq-dc1",))

    assert residency_refusal(Residency(), None) is None
    assert residency_refusal(zoned, eu) is None
    assert residency_refusal(zoned, None) == "data_boundary_unknown"
    assert residency_refusal(zoned, unknown_zone) == "stamp_outside_data_zone"
    assert residency_refusal(regional, unknown_zone) is None
    assert residency_refusal(regional, eu) == "stamp_region_not_permitted"

    assert zone_refusal(None, None) is None
    assert zone_refusal("eu", eu) is None
    assert zone_refusal("us", eu) == "stamp_outside_data_zone"
    assert zone_refusal("eu", None) == "data_boundary_unknown"


def test_boundary_is_read_from_the_document() -> None:
    assert boundary_from_payload({}) is None
    assert boundary_from_payload({"data_boundary": EU_BOUNDARY}) == DataBoundary(
        region="westeurope", data_zone="eu"
    )
    registry = DeploymentRegistry.from_payload(document(EU_BOUNDARY))
    assert registry.reported_data_boundary() == DataBoundary("westeurope", "eu")


def test_a_reloading_registry_follows_a_moved_stamp(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "deployments.json"
    path.write_text(json.dumps(document(EU_BOUNDARY)))
    registry = ReloadingRegistry(path, min_interval=0)
    assert registry.reported_data_boundary() == DataBoundary("westeurope", "eu")

    moved = json.dumps(document({"region": "eastus", "data_zone": "us"}))
    path.write_text(moved + " " * 8)  # a different size guarantees the reload is noticed
    assert registry.reported_data_boundary() == DataBoundary("eastus", "us")
    assert registry.snapshot()["data_boundary"] == {"region": "eastus", "data_zone": "us"}


def test_a_malformed_zone_claim_is_rejected(
    control_plane: ControlPlaneStub, signing_key: SigningKey
) -> None:
    settings = make_settings()
    keys = KeyCache(settings, client=control_plane.client())
    principal = verify_inference_token(
        token(signing_key, data_zone=" EU "), settings=settings, keys=keys
    )
    assert principal.data_zone == "eu"
    with pytest.raises(Unauthorized):
        verify_inference_token(token(signing_key, data_zone=7), settings=settings, keys=keys)


# --- deployment residency -----------------------------------------------------


async def test_an_in_zone_deployment_is_served(
    make_client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    http, _plane = await make_client(EU_BOUNDARY)
    response = await http.post(
        "/v1/chat/completions", json=chat("eu-model"), headers=auth(token(signing_key))
    )
    assert response.status_code == 200, response.text
    assert len(upstream.requests) == 1


async def test_a_deployment_this_stamp_may_not_serve_is_refused_before_the_host(
    make_client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    """The stamp moved out of the deployment's region after placement."""
    http, plane = await make_client(EU_BOUNDARY)
    response = await http.post(
        "/v1/chat/completions", json=chat("us-model"), headers=auth(token(signing_key))
    )

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "data_residency_not_satisfied"
    assert error["details"]["reason"] == "stamp_region_not_permitted"
    assert upstream.requests == []
    assert (
        'fabric_dp_data_zone_refusals_total{binding="deployment",'
        'reason="stamp_region_not_permitted"} 1'
    ) in plane.metrics.render()


async def test_without_a_boundary_only_global_deployments_are_served(
    make_client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    """An agent that predates data zones writes no boundary; restricted work fails closed."""
    http, _plane = await make_client(None)
    refused = await http.post(
        "/v1/chat/completions", json=chat("eu-model"), headers=auth(token(signing_key))
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["details"]["reason"] == "data_boundary_unknown"

    served = await http.post(
        "/v1/chat/completions", json=chat("open-model"), headers=auth(token(signing_key))
    )
    assert served.status_code == 200, served.text


async def test_auto_never_selects_a_deployment_this_stamp_may_not_serve(
    make_client, signing_key: SigningKey
) -> None:
    """us-model has the highest priority, so without the filter it would win."""
    http, _plane = await make_client(EU_BOUNDARY)
    response = await http.post(
        "/v1/chat/completions",
        json=chat("auto", routing={"explain": True}),
        headers=auth(token(signing_key)),
    )
    assert response.status_code == 200, response.text
    assert response.headers["x-fabric-selected-model"] == "eu-model"


async def test_models_lists_only_what_this_stamp_may_serve(
    make_client, signing_key: SigningKey
) -> None:
    http, _plane = await make_client(EU_BOUNDARY)
    response = await http.get("/v1/models", headers=auth(token(signing_key)))
    assert response.status_code == 200
    assert sorted(item["id"] for item in response.json()["data"]) == ["eu-model", "open-model"]


# --- zone-bound tokens and requests -------------------------------------------


async def test_a_zone_bound_token_is_served_in_zone_and_refused_outside_it(
    make_client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    http, plane = await make_client(EU_BOUNDARY)
    served = await http.post(
        "/v1/chat/completions",
        json=chat("open-model"),
        headers=auth(token(signing_key, data_zone="eu")),
    )
    assert served.status_code == 200, served.text

    refused = await http.post(
        "/v1/chat/completions",
        json=chat("open-model"),
        headers=auth(token(signing_key, data_zone="us")),
    )
    assert refused.status_code == 403
    error = refused.json()["error"]
    assert error["code"] == "data_zone_mismatch"
    assert error["details"] == {
        "reason": "stamp_outside_data_zone",
        "binding": "token",
        "required_data_zone": "us",
        "stamp_data_zone": "eu",
    }
    assert len(upstream.requests) == 1
    assert (
        'fabric_dp_data_zone_refusals_total{binding="token",reason="stamp_outside_data_zone"} 1'
    ) in plane.metrics.render()

    listing = await http.get("/v1/models", headers=auth(token(signing_key, data_zone="us")))
    assert listing.status_code == 403


async def test_a_zone_bound_token_is_refused_by_a_stamp_with_no_boundary(
    make_client, signing_key: SigningKey
) -> None:
    http, _plane = await make_client(None)
    refused = await http.post(
        "/v1/chat/completions",
        json=chat("open-model"),
        headers=auth(token(signing_key, data_zone="eu")),
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["details"]["reason"] == "data_boundary_unknown"


async def test_a_zone_bound_audio_request_is_refused_before_the_upload_is_read(
    make_client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    http, _plane = await make_client(EU_BOUNDARY)
    refused = await http.post(
        "/v1/audio/transcriptions",
        data={"model": "open-model"},
        files={"file": ("a.wav", b"RIFF....", "audio/wav")},
        headers=auth(token(signing_key, data_zone="jp")),
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "data_zone_mismatch"
    assert upstream.requests == []


async def test_a_request_can_require_a_zone_and_the_hint_is_not_forwarded(
    make_client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    http, _plane = await make_client(EU_BOUNDARY)
    served = await http.post(
        "/v1/chat/completions",
        json=chat("open-model", routing={"data_zone": "EU"}),
        headers=auth(token(signing_key)),
    )
    assert served.status_code == 200, served.text
    assert "routing" not in upstream.requests[0]["payload"]

    refused = await http.post(
        "/v1/chat/completions",
        json=chat("open-model", routing={"data_zone": "us"}),
        headers=auth(token(signing_key)),
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["details"]["binding"] == "request"

    malformed = await http.post(
        "/v1/chat/completions",
        json=chat("open-model", routing={"data_zone": 3}),
        headers=auth(token(signing_key)),
    )
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "invalid_routing"
