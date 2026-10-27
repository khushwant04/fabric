"""Data-zone residency (ADR 0017).

A deployment declares where its requests may be processed: anywhere (``global``), inside one
data zone, or in an explicit list of regions. These tests cover the control-plane halves of
that promise: placement refuses stamps outside the boundary, an edit cannot tighten the
boundary around a stamp already serving, tokens can be bound to a zone, the stamp learns its
own boundary through desired state, and clients can discover in-zone routes.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.core import data_zones
from app.core.jwt_service import decode_token
from tests.helpers import (
    bearer,
    default_capabilities,
    enroll_stamp,
    onboard,
    place,
    report_capabilities,
)


def _error(response) -> dict:
    return response.json()["error"]


async def _deployment(
    client: AsyncClient,
    account_id: str,
    token: str,
    *,
    residency: dict[str, Any] | None,
    name: str = "primary",
    alias: str = "launch-model",
) -> Any:
    spec: dict[str, Any] = {"runtime": {"release": "runtime-release-1"}}
    if residency is not None:
        spec["residency"] = residency
    return await client.post(
        f"/v1/accounts/{account_id}/deployments",
        json={"name": name, "model_alias": alias, "spec": spec},
        headers=bearer(token),
    )


async def _stamp(
    client: AsyncClient, account_id: str, token: str, *, name: str, region: str | None
) -> dict[str, Any]:
    capabilities = default_capabilities(gpus=4, region=region)
    capabilities["inference_url"] = f"https://{name}.inference.example"
    return await enroll_stamp(client, account_id, token, name=name, capabilities=capabilities)


# --- catalogue --------------------------------------------------------------


def test_every_region_belongs_to_at_most_one_zone() -> None:
    """Overlapping zones would make membership depend on which zone was asked about."""
    seen: dict[str, str] = {}
    for key, zone in data_zones.DATA_ZONES.items():
        for region in zone.regions:
            assert region not in seen, f"{region} is in {seen.get(region)} and {key}"
            assert data_zones.REGION_PATTERN.match(region), region
            seen[region] = key


@pytest.mark.parametrize(
    ("region", "zone"),
    [
        ("westeurope", "eu"),
        ("eu-central-1", "eu"),
        ("europe-west4", "eu"),
        ("uksouth", "uk"),
        ("eu-west-2", "uk"),
        ("eu-central-2", "ch"),
        ("EastUS", "us"),
        ("us_east_1", "us"),
        ("southeastasia", "apac"),
        ("japaneast", "jp"),
        ("my-basement", None),
        (None, None),
    ],
)
def test_regions_map_to_zones(region: str | None, zone: str | None) -> None:
    assert data_zones.zone_of(region) == zone


def test_an_unreadable_stored_policy_is_read_as_the_strictest() -> None:
    """Only a direct database edit can produce this; reading it as global would be the leak."""
    damaged = data_zones.residency_of({"residency": {"level": "data_zone", "data_zone": "xx"}})
    assert damaged.refusal("westeurope") is not None
    assert damaged.refusal("eastus") is not None
    assert data_zones.residency_of({}).refusal(None) is None


async def test_catalogue_is_public(client: AsyncClient) -> None:
    response = await client.get("/v1/data-zones")
    assert response.status_code == 200
    body = response.json()
    assert body["levels"] == ["global", "data_zone", "regional"]
    eu = next(zone for zone in body["zones"] if zone["key"] == "eu")
    assert "westeurope" in eu["regions"]


# --- spec validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "residency",
    [
        {"level": "data_zone"},
        {"level": "data_zone", "data_zone": "atlantis"},
        {"level": "data_zone", "data_zone": "eu", "regions": ["westeurope"]},
        {"level": "regional"},
        {"level": "regional", "regions": ["not a region"]},
        {"level": "regional", "regions": ["westeurope"], "data_zone": "eu"},
        {"level": "global", "data_zone": "eu"},
        {"level": "planetary"},
    ],
)
async def test_inconsistent_residency_is_refused(
    client: AsyncClient, residency: dict[str, Any]
) -> None:
    account_id, token = await onboard(client, "dz-invalid", "dz-invalid-account")
    response = await _deployment(client, account_id, token, residency=residency)
    assert response.status_code == 422, response.text


async def test_residency_is_normalised_and_defaults_to_global(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "dz-norm", "dz-norm-account")
    legacy = await _deployment(client, account_id, token, residency=None, name="legacy")
    assert legacy.status_code == 201
    assert legacy.json()["desired_spec"]["residency"]["level"] == "global"

    regional = await _deployment(
        client,
        account_id,
        token,
        residency={"level": "regional", "regions": ["WestEurope", "westeurope", "eu_west_1"]},
        name="regional",
    )
    assert regional.status_code == 201, regional.text
    assert regional.json()["desired_spec"]["residency"]["regions"] == ["eu-west-1", "westeurope"]


# --- placement ----------------------------------------------------------------


async def test_a_data_zone_deployment_is_refused_on_an_out_of_zone_stamp(
    client: AsyncClient,
) -> None:
    account_id, token = await onboard(client, "dz-named", "dz-named-account")
    created = await _deployment(
        client, account_id, token, residency={"level": "data_zone", "data_zone": "eu"}
    )
    us = await _stamp(client, account_id, token, name="us-stamp", region="eastus")

    refused = await place(
        client, account_id, token, created.json()["id"], stamp_id=us["stamp"]["id"]
    )

    assert refused.status_code == 409, refused.text
    details = _error(refused)["details"]
    assert details["reason"] == "data_residency_not_satisfied"
    assert details["residency_reason"] == "stamp_outside_data_zone"
    assert details["stamp_data_zone"] == "us"
    assert details["demand"]["residency"] == {
        "level": "data_zone",
        "data_zone": "eu",
        "regions": [],
    }


async def test_selection_only_considers_in_zone_stamps(client: AsyncClient) -> None:
    """The out-of-zone stamp is larger, so without residency it would win selection."""
    account_id, token = await onboard(client, "dz-select", "dz-select-account")
    created = await _deployment(
        client, account_id, token, residency={"level": "data_zone", "data_zone": "eu"}
    )
    await enroll_stamp(
        client,
        account_id,
        token,
        name="big-us",
        capabilities=default_capabilities(gpus=8, region="eastus"),
    )
    eu = await _stamp(client, account_id, token, name="small-eu", region="westeurope")

    placed = await place(client, account_id, token, created.json()["id"])

    assert placed.status_code == 201, placed.text
    assert placed.json()["stamp_id"] == eu["stamp"]["id"]


async def test_no_in_zone_stamp_names_the_reason(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "dz-none", "dz-none-account")
    created = await _deployment(
        client, account_id, token, residency={"level": "data_zone", "data_zone": "jp"}
    )
    await _stamp(client, account_id, token, name="eu-only", region="westeurope")

    refused = await place(client, account_id, token, created.json()["id"])

    assert refused.status_code == 409
    error = _error(refused)
    assert error["code"] == "no_stamp_fits_deployment"
    assert error["details"]["candidates"][0]["reason"] == "data_residency_not_satisfied"


async def test_an_uncatalogued_region_satisfies_only_global_and_regional(
    client: AsyncClient,
) -> None:
    account_id, token = await onboard(client, "dz-onprem", "dz-onprem-account")
    stamp = await _stamp(client, account_id, token, name="basement", region="hq-dc1")
    zone = await _deployment(
        client, account_id, token, residency={"level": "data_zone", "data_zone": "eu"}
    )
    named = await _deployment(
        client,
        account_id,
        token,
        residency={"level": "regional", "regions": ["hq-dc1"]},
        name="regional",
        alias="regional-model",
    )

    refused = await place(
        client, account_id, token, zone.json()["id"], stamp_id=stamp["stamp"]["id"]
    )
    assert refused.status_code == 409
    assert _error(refused)["details"]["residency_reason"] == "stamp_region_not_in_catalogue"

    placed = await place(
        client, account_id, token, named.json()["id"], stamp_id=stamp["stamp"]["id"]
    )
    assert placed.status_code == 201, placed.text


async def test_a_stamp_without_a_region_cannot_hold_a_restricted_deployment(
    client: AsyncClient,
) -> None:
    account_id, token = await onboard(client, "dz-noregion", "dz-noregion-account")
    stamp = await _stamp(client, account_id, token, name="nowhere", region=None)
    created = await _deployment(
        client, account_id, token, residency={"level": "regional", "regions": ["westeurope"]}
    )

    refused = await place(
        client, account_id, token, created.json()["id"], stamp_id=stamp["stamp"]["id"]
    )

    assert refused.status_code == 409
    assert _error(refused)["details"]["residency_reason"] == "stamp_region_unknown"


# --- editing --------------------------------------------------------------------


async def test_tightening_residency_around_a_serving_stamp_is_refused(
    client: AsyncClient,
) -> None:
    account_id, token = await onboard(client, "dz-tighten", "dz-tighten-account")
    created = await _deployment(client, account_id, token, residency=None)
    us = await _stamp(client, account_id, token, name="us-stamp", region="eastus")
    placed = await place(
        client, account_id, token, created.json()["id"], stamp_id=us["stamp"]["id"]
    )
    assert placed.status_code == 201

    deployment_id = created.json()["id"]
    refused = await client.patch(
        f"/v1/accounts/{account_id}/deployments/{deployment_id}",
        json={
            "spec": {
                "runtime": {"release": "runtime-release-1"},
                "residency": {"level": "data_zone", "data_zone": "eu"},
            }
        },
        headers=bearer(token),
    )

    assert refused.status_code == 409, refused.text
    error = _error(refused)
    assert error["code"] == "residency_excludes_existing_placement"
    assert error["details"]["placements"][0]["stamp_id"] == us["stamp"]["id"]

    # Tightening to a boundary the stamp is inside is fine.
    accepted = await client.patch(
        f"/v1/accounts/{account_id}/deployments/{deployment_id}",
        json={
            "spec": {
                "runtime": {"release": "runtime-release-1"},
                "residency": {"level": "data_zone", "data_zone": "us"},
            }
        },
        headers=bearer(token),
    )
    assert accepted.status_code == 200, accepted.text


# --- desired state ----------------------------------------------------------------


async def test_desired_state_carries_the_boundary_and_follows_a_moved_stamp(
    client: AsyncClient,
) -> None:
    account_id, token = await onboard(client, "dz-boundary", "dz-boundary-account")
    enrolled = await _stamp(client, account_id, token, name="moving", region="westeurope")
    stamp_id = enrolled["stamp"]["id"]
    agent = enrolled["agent_credential"]

    first = await client.get(
        f"/v1/stamps/{stamp_id}/desired-state?after_generation=0", headers=bearer(agent)
    )
    assert first.status_code == 200
    assert first.json()["data_boundary"] == {"region": "westeurope", "data_zone": "eu"}

    moved = default_capabilities(gpus=4, region="eastus")
    assert (await report_capabilities(client, stamp_id, agent, moved)).status_code == 200

    second = await client.get(
        f"/v1/stamps/{stamp_id}/desired-state?after_generation=0", headers=bearer(agent)
    )
    assert second.json()["data_boundary"] == {"region": "eastus", "data_zone": "us"}


# --- tokens -----------------------------------------------------------------------


async def _inference_key(
    client: AsyncClient, account_id: str, token: str, *, data_zone: str | None
) -> dict[str, Any]:
    body: dict[str, Any] = {"name": "zone-key", "scopes": ["inference:invoke"]}
    if data_zone is not None:
        body["data_zone"] = data_zone
    response = await client.post(
        f"/v1/accounts/{account_id}/api-keys", json=body, headers=bearer(token)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _exchange(client: AsyncClient, secret: str, **extra: Any):
    return await client.post(
        "/v1/token",
        json={
            "grant_type": "api_key",
            "api_key": secret,
            "audience": "fabric-inference",
            **extra,
        },
    )


async def test_a_zone_bound_key_mints_zone_bound_tokens(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "dz-key", "dz-key-account")
    created = await _inference_key(client, account_id, token, data_zone="EU")
    assert created["api_key"]["data_zone"] == "eu"

    exchanged = await _exchange(client, created["secret"])
    assert exchanged.status_code == 200, exchanged.text
    assert exchanged.json()["data_zone"] == "eu"
    claims = decode_token(exchanged.json()["access_token"], audience="fabric-inference")
    assert claims["data_zone"] == "eu"

    # Asking for the same zone is fine; asking for another is refused, not overridden.
    assert (await _exchange(client, created["secret"], data_zone="eu")).status_code == 200
    widened = await _exchange(client, created["secret"], data_zone="us")
    assert widened.status_code == 403
    assert _error(widened)["code"] == "data_zone_not_permitted"


async def test_an_unbound_key_can_narrow_per_token(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "dz-narrow", "dz-narrow-account")
    created = await _inference_key(client, account_id, token, data_zone=None)

    plain = await _exchange(client, created["secret"])
    assert "data_zone" not in decode_token(
        plain.json()["access_token"], audience="fabric-inference"
    )

    narrowed = await _exchange(client, created["secret"], data_zone="jp")
    assert narrowed.status_code == 200
    claims = decode_token(narrowed.json()["access_token"], audience="fabric-inference")
    assert claims["data_zone"] == "jp"


async def test_zone_binding_is_refused_for_control_tokens_and_unknown_zones(
    client: AsyncClient,
) -> None:
    account_id, token = await onboard(client, "dz-control", "dz-control-account")
    control = await client.post(
        "/v1/token",
        json={
            "grant_type": "auth0_token",
            "assertion": "user:dz-control",
            "audience": "fabric-control",
            "data_zone": "eu",
        },
    )
    assert control.status_code == 400
    assert _error(control)["code"] == "data_zone_requires_inference_audience"

    unknown = await client.post(
        f"/v1/accounts/{account_id}/api-keys",
        json={"name": "bad", "scopes": ["inference:invoke"], "data_zone": "atlantis"},
        headers=bearer(token),
    )
    assert unknown.status_code == 422


# --- route discovery ------------------------------------------------------------------


async def test_routes_are_filtered_by_zone_and_ordered_by_health(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "dz-routes", "dz-routes-account")
    created = await _deployment(client, account_id, token, residency=None)
    deployment_id = created.json()["id"]
    eu = await _stamp(client, account_id, token, name="eu-a", region="westeurope")
    eu_ready = await _stamp(client, account_id, token, name="eu-b", region="northeurope")
    us = await _stamp(client, account_id, token, name="us-a", region="eastus")
    for enrolled in (eu, eu_ready, us):
        placed = await place(
            client, account_id, token, deployment_id, stamp_id=enrolled["stamp"]["id"]
        )
        assert placed.status_code == 201, placed.text

    # eu-b reports the deployment ready; eu-a is still rolling out.
    generation = (
        await client.get(
            f"/v1/stamps/{eu_ready['stamp']['id']}/desired-state?after_generation=0",
            headers=bearer(eu_ready["agent_credential"]),
        )
    ).json()["deployments"][0]["desired_generation"]
    reported = await client.post(
        f"/v1/stamps/{eu_ready['stamp']['id']}/status",
        json={
            "deployment_id": deployment_id,
            "observed_generation": generation,
            "phase": "ready",
            "ready_replicas": 1,
            "unavailable_replicas": 0,
            "conditions": [],
        },
        headers=bearer(eu_ready["agent_credential"]),
    )
    assert reported.status_code in (200, 202), reported.text

    response = await client.get(
        f"/v1/accounts/{account_id}/inference-routes",
        params={"model": "launch-model", "data_zone": "eu"},
        headers=bearer(token),
    )

    assert response.status_code == 200, response.text
    routes = response.json()["routes"]
    assert [route["stamp_id"] for route in routes] == [
        eu_ready["stamp"]["id"],
        eu["stamp"]["id"],
    ]
    assert routes[0]["healthy"] is True
    assert routes[0]["inference_url"] == "https://eu-b.inference.example"
    assert routes[1]["healthy"] is False
    assert {route["data_zone"] for route in routes} == {"eu"}

    everywhere = await client.get(
        f"/v1/accounts/{account_id}/inference-routes",
        params={"model": deployment_id},
        headers=bearer(token),
    )
    assert len(everywhere.json()["routes"]) == 3

    unknown = await client.get(
        f"/v1/accounts/{account_id}/inference-routes",
        params={"model": "launch-model", "data_zone": "atlantis"},
        headers=bearer(token),
    )
    assert unknown.status_code == 400


async def test_a_stamp_that_left_the_zone_is_not_offered_as_a_route(
    client: AsyncClient,
) -> None:
    account_id, token = await onboard(client, "dz-drift", "dz-drift-account")
    created = await _deployment(
        client, account_id, token, residency={"level": "data_zone", "data_zone": "eu"}
    )
    enrolled = await _stamp(client, account_id, token, name="drifter", region="westeurope")
    placed = await place(
        client, account_id, token, created.json()["id"], stamp_id=enrolled["stamp"]["id"]
    )
    assert placed.status_code == 201

    moved = default_capabilities(gpus=4, region="eastus")
    await report_capabilities(client, enrolled["stamp"]["id"], enrolled["agent_credential"], moved)

    default = await client.get(
        f"/v1/accounts/{account_id}/inference-routes",
        params={"model": "launch-model"},
        headers=bearer(token),
    )
    assert default.json()["routes"] == []

    diagnostic = await client.get(
        f"/v1/accounts/{account_id}/inference-routes",
        params={"model": "launch-model", "include_ineligible": True},
        headers=bearer(token),
    )
    [route] = diagnostic.json()["routes"]
    assert route["residency_satisfied"] is False
    assert route["healthy"] is False
