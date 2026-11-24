"""Routing policies at the edge: target choice, agreement with stamps, stamp preference."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from fabric_data_plane import routing_policy as rp
from tests.conftest import ACCOUNT_A, ACCOUNT_B, SigningKey
from tests.test_edge import (
    DEP_ONLY_A,
    DEP_SHARED,
    STAMP_B,
    ControlPlane,
    Stamps,
    build_edge,
    chat,
    client_for,
    placement,
    table_document,
)

DEP_ONLY_B = uuid.UUID("d0000000-0000-0000-0000-0000000000b0")


@pytest.fixture(name="signer")
def signer_fixture() -> SigningKey:
    return SigningKey()


@pytest.fixture(name="stamps")
def stamps_fixture() -> Stamps:
    return Stamps()


@pytest.fixture(name="control")
def control_fixture(signer: SigningKey) -> ControlPlane:
    return ControlPlane(signer)


def policy_document(**default: Any) -> dict[str, Any]:
    return {
        "policy_id": str(uuid.uuid4()),
        "account_id": str(ACCOUNT_A),
        "alias": "chat-default",
        "version": 2,
        "rules": default.pop("rules", []),
        "default": {"id": "default", "targets": [], **default},
    }


def targets(*pairs: tuple[uuid.UUID, int]) -> list[dict[str, Any]]:
    return [{"deployment_id": str(d), "weight": w} for d, w in pairs]


def with_policy(control: ControlPlane, policy: dict[str, Any]) -> None:
    document = table_document()
    document["deployments"].append(
        {
            "account_id": str(ACCOUNT_A),
            "deployment_id": str(DEP_ONLY_B),
            "name": "only-b",
            "model_alias": "b-only-model",
            "capabilities": {},
            "placements": [placement(STAMP_B, "stamp-b", "eu-west")],
        }
    )
    document["routing_policies"] = [policy]
    control.document = document


async def test_the_alias_goes_to_a_stamp_holding_the_target(control, stamps, signer) -> None:
    with_policy(control, policy_document(targets=targets((DEP_ONLY_B, 100))))
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        response = await chat(client, signer, "chat-default")
    assert response.status_code == 200, response.text
    assert response.json() == {"served_by": "stamp-b"}
    # The caller's bytes are forwarded unchanged: the stamp evaluates the policy again.
    assert json.loads(stamps.seen[0]["body"])["model"] == "chat-default"


async def test_the_edge_and_the_stamp_draw_the_same_target(control, stamps, signer) -> None:
    policy = policy_document(targets=targets((DEP_ONLY_A, 50), (DEP_ONLY_B, 50)))
    with_policy(control, policy)
    edge = build_edge(control, stamps)
    rule = rp.Policy.from_document(policy).default
    where = {str(DEP_ONLY_A): "stamp-a", str(DEP_ONLY_B): "stamp-b"}
    async with client_for(edge) as client:
        for _ in range(30):
            await chat(client, signer, "chat-default")
            seen = stamps.seen[-1]
            key = seen["headers"][rp.ROUTE_KEY_HEADER]
            # The stamp draws with the same key; the edge sent the request where that lands.
            assert where[rp.choose_target(rule, f"route:{key}")] == seen["host"]
        assert {item["host"] for item in stamps.seen} == {"stamp-a", "stamp-b"}

        # A session key is used as it is, and no route key is added.
        for _ in range(3):
            await chat(client, signer, "chat-default", headers={"X-Fabric-Session": "s-1"})
        last = stamps.seen[-3:]
        assert len({item["host"] for item in last}) == 1
        assert all(rp.ROUTE_KEY_HEADER not in item["headers"] for item in last)
        assert where[rp.choose_target(rule, "session:s-1")] == last[0]["host"]


async def test_fallback_deployments_stamps_are_tried_after_the_targets(
    control, stamps, signer
) -> None:
    with_policy(
        control, policy_document(targets=targets((DEP_ONLY_A, 100)), fallbacks=[str(DEP_ONLY_B)])
    )
    stamps.down = {"stamp-a"}
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        response = await chat(client, signer, "chat-default")
    assert response.status_code == 200
    assert response.json() == {"served_by": "stamp-b"}


@pytest.mark.parametrize("mode", ["prefer", "pin"])
async def test_stamp_preference(control, stamps, signer, mode) -> None:
    with_policy(
        control,
        policy_document(
            targets=targets((DEP_SHARED, 100)),
            stamp_preference={"mode": mode, "stamp_ids": [STAMP_B]},
        ),
    )
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        for _ in range(5):
            response = await chat(client, signer, "chat-default",
                                  headers={"X-Fabric-Region": "us-east"})
            assert response.json() == {"served_by": "stamp-b"}
        stamps.down = {"stamp-b"}
        response = await chat(client, signer, "chat-default")
    if mode == "prefer":
        assert response.json() == {"served_by": "stamp-a"}
    else:
        assert response.status_code == 503
        assert stamps.count("stamp-a") == 0


async def test_a_pin_to_a_stamp_without_the_target_is_refused(control, stamps, signer) -> None:
    with_policy(
        control,
        policy_document(
            targets=targets((DEP_ONLY_A, 100)),
            stamp_preference={"mode": "pin", "stamp_ids": [STAMP_B]},
        ),
    )
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        response = await chat(client, signer, "chat-default")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_stamp_for_policy"
    assert stamps.seen == []


async def test_other_accounts_and_names_are_unaffected(control, stamps, signer) -> None:
    with_policy(control, policy_document(targets=targets((DEP_ONLY_B, 100))))
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        theirs = await chat(client, signer, "chat-default", account=ACCOUNT_B)
        plain = await chat(client, signer, "a-model")
        models = await client.get("/v1/models", headers={
            "Authorization": f"Bearer {signer.issue(account_id=ACCOUNT_A)}"})
    assert theirs.status_code == 404
    assert plain.json() == {"served_by": "stamp-a"}
    assert rp.ROUTE_KEY_HEADER not in stamps.seen[-1]["headers"]
    assert "chat-default" in [item["id"] for item in models.json()["data"]]


async def test_policies_keep_routing_with_the_control_plane_down(
    control, stamps, signer, tmp_path
) -> None:
    with_policy(control, policy_document(targets=targets((DEP_ONLY_B, 100))))
    edge = build_edge(control, stamps, tmp_path=tmp_path)
    edge.store.install(control.document, source="control-plane")  # persisted to disk
    control.offline = True
    control.calls.clear()
    restarted = build_edge(control, stamps, tmp_path=tmp_path, install=False)
    restarted.keys = edge.keys
    assert restarted.store.load_from_disk()
    async with client_for(restarted) as client:
        response = await chat(client, signer, "chat-default")
    assert response.json() == {"served_by": "stamp-b"}
    assert not [call for call in control.calls if "routing-table" in call]


async def test_a_stamp_without_policy_support_gets_the_target_alias(
    control, stamps, signer
) -> None:
    """A stamp on a release older than routing policies answers the policy name with
    model_not_found; the edge asks it once more by the alias of the target it chose."""
    with_policy(control, policy_document(targets=targets((DEP_ONLY_B, 100))))
    original = stamps.handler

    def old_stamp(request):
        model = json.loads(request.content)["model"]
        if request.headers.get("host", "").startswith("stamp-b") and model == "chat-default":
            stamps.seen.append(
                {"host": "stamp-b", "body": request.content, "headers": dict(request.headers)}
            )
            error = {"code": "model_not_found", "message": "No such model is served by this stamp"}
            return httpx.Response(404, json={"error": error})
        return original(request)

    stamps.handler = old_stamp
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        response = await chat(client, signer, "chat-default")
    assert response.status_code == 200, response.text
    assert response.json() == {"served_by": "stamp-b"}
    first, second = stamps.seen[-2], stamps.seen[-1]
    assert json.loads(first["body"])["model"] == "chat-default"
    assert json.loads(second["body"])["model"] == "b-only-model"
    assert second["headers"]["x-fabric-policy-degraded"] == "edge-only"


async def test_other_not_found_answers_are_not_retried(control, stamps, signer) -> None:
    with_policy(control, policy_document(targets=targets((DEP_ONLY_B, 100))))
    stamps.status = {"stamp-b": 404}
    edge = build_edge(control, stamps)
    async with client_for(edge) as client:
        response = await chat(client, signer, "chat-default")
    assert response.status_code == 404
    assert stamps.count("stamp-b") == 1
