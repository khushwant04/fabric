"""Routing policies: drafts, validation, publish, rollback, simulate, isolation, delivery.

Runs on SQLite by default and on PostgreSQL with ``FABRIC_TEST_DATABASE_URL``, where the
isolation tests also exercise row-level security.
"""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import reset_context, system_context
from app.models import AuditEvent
from tests.helpers import bearer, enroll_stamp, onboard, place
from tests.test_edge_routing import TABLE, capabilities_with_url, edge_token

REPO = pathlib.Path(__file__).resolve().parents[2]
DATA_PLANE_COPY = REPO / "data-plane" / "fabric_data_plane" / "routing_policy.py"


def data_plane_module():
    """The data plane's own copy, loaded from its file (it imports only the stdlib)."""
    spec = importlib.util.spec_from_file_location("data_plane_routing_policy", DATA_PLANE_COPY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def policies(account_id: str) -> str:
    return f"/v1/accounts/{account_id}/routing-policies"


async def deployment(
    client: AsyncClient,
    account_id: str,
    token: str,
    *,
    name: str,
    alias: str | None = None,
    release: str = "runtime-release-1",
    tool_parser: str | None = None,
) -> dict[str, Any]:
    runtime: dict[str, Any] = {"release": release}
    if tool_parser:
        runtime["tool_call_parser"] = tool_parser
    response = await client.post(
        f"/v1/accounts/{account_id}/deployments",
        json={"name": name, "model_alias": alias or name, "spec": {"runtime": runtime}},
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def document(
    default_targets: list[tuple[str, int]],
    *,
    rules: list[dict[str, Any]] | None = None,
    fallbacks: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "rules": rules or [],
        "default": {
            "id": "default",
            "name": "Default",
            "targets": [{"deployment_id": d, "weight": w} for d, w in default_targets],
            "fallbacks": fallbacks or [],
        },
    }


async def create_policy(
    client: AsyncClient, account_id: str, token: str, alias: str, draft: dict[str, Any]
) -> dict[str, Any]:
    response = await client.post(
        policies(account_id),
        json={"alias": alias, "description": "test", "draft": draft},
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def account(client: AsyncClient) -> dict[str, Any]:
    account_id, token = await onboard(client, "rp-owner", "rp-owner-account")
    stable = await deployment(client, account_id, token, name="chat-stable",
                              release="Qwen/Qwen3.5-4B")
    canary = await deployment(client, account_id, token, name="chat-canary")
    big = await deployment(client, account_id, token, name="chat-big")
    reset_context()
    return {
        "id": account_id,
        "token": token,
        "stable": stable["id"],
        "canary": canary["id"],
        "big": big["id"],
    }


def test_the_policy_module_is_identical_in_both_planes() -> None:
    """Simulation and enforcement run the same file, so they cannot drift."""
    control = REPO / "control-plane" / "app" / "core" / "routing_policy.py"
    data = REPO / "data-plane" / "fabric_data_plane" / "routing_policy.py"
    assert data.exists(), "the data-plane copy is missing"
    assert hashlib.sha256(control.read_bytes()).hexdigest() == hashlib.sha256(
        data.read_bytes()
    ).hexdigest(), "copy control-plane/app/core/routing_policy.py over the data-plane one"


async def test_create_save_publish_and_read(client: AsyncClient, account) -> None:
    acc, token = account["id"], account["token"]
    created = await create_policy(
        client, acc, token, "chat-default", document([(account["stable"], 100)])
    )
    assert created["live_version"] is None
    assert created["has_unpublished_changes"] is True
    assert created["validation"]["valid"] is True

    draft = document(
        [(account["stable"], 90), (account["canary"], 10)],
        rules=[
            {
                "id": "tools",
                "name": "Tool calls",
                "match": {"has_tools": True, "endpoints": ["chat_completions"]},
                "targets": [{"deployment_id": account["big"], "weight": 100}],
                "fallbacks": [account["stable"]],
                "fallback_on": ["server_error", "timeout"],
                "strategy": "prefix_aware",
                "timeout_seconds": 20,
            }
        ],
    )
    saved = await client.put(
        f"{policies(acc)}/{created['id']}/draft", json={"draft": draft}, headers=bearer(token)
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["draft"]["rules"][0]["id"] == "tools"

    published = await client.post(
        f"{policies(acc)}/{created['id']}/publish", json={"note": "canary 10%"},
        headers=bearer(token),
    )
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["live_version"] == 1
    assert body["has_unpublished_changes"] is False
    assert body["live"]["note"] == "canary 10%"
    assert body["live"]["published_by"]

    listed = await client.get(policies(acc), headers=bearer(token))
    assert listed.status_code == 200
    [summary] = listed.json()
    assert summary["alias"] == "chat-default"
    assert summary["live_version"] == 1
    assert summary["rule_count"] == 2
    assert set(summary["deployment_ids"]) == {account["stable"], account["canary"], account["big"]}


async def test_versions_are_immutable_and_rollback_republishes(
    client: AsyncClient, account
) -> None:
    acc, token = account["id"], account["token"]
    policy = await create_policy(
        client, acc, token, "chat-default", document([(account["stable"], 100)])
    )
    url = f"{policies(acc)}/{policy['id']}"
    assert (await client.post(f"{url}/publish", json={}, headers=bearer(token))).status_code == 200
    await client.put(
        f"{url}/draft",
        json={"draft": document([(account["stable"], 50), (account["canary"], 50)])},
        headers=bearer(token),
    )
    second = await client.post(f"{url}/publish", json={"note": "50/50"}, headers=bearer(token))
    assert second.json()["live_version"] == 2

    # Editing the draft does not touch what is live.
    await client.put(
        f"{url}/draft", json={"draft": document([(account["big"], 100)])}, headers=bearer(token)
    )
    current = (await client.get(url, headers=bearer(token))).json()
    assert current["live_version"] == 2
    assert current["has_unpublished_changes"] is True
    assert len(current["live"]["document"]["default"]["targets"]) == 2

    rolled = await client.post(
        f"{url}/rollback", json={"version": 1}, headers=bearer(token)
    )
    assert rolled.status_code == 200, rolled.text
    body = rolled.json()
    assert body["live_version"] == 3
    assert body["live"]["rolled_back_from"] == 1
    assert body["live"]["document"]["default"]["targets"] == [
        {"deployment_id": account["stable"], "weight": 100}
    ]
    assert body["has_unpublished_changes"] is False

    versions = (await client.get(f"{url}/versions", headers=bearer(token))).json()
    assert [item["version"] for item in versions] == [3, 2, 1]
    one = (await client.get(f"{url}/versions/2", headers=bearer(token))).json()
    assert one["note"] == "50/50"
    missing = await client.get(f"{url}/versions/9", headers=bearer(token))
    assert missing.status_code == 404


async def test_validation_blocks_publishing(client: AsyncClient, account) -> None:
    acc, token = account["id"], account["token"]
    policy = await create_policy(
        client, acc, token, "chat-default", document([(account["stable"], 60)])
    )
    report = policy["validation"]
    assert not report["valid"]
    assert [issue["code"] for issue in report["errors"]] == ["weights_sum"]
    refused = await client.post(
        f"{policies(acc)}/{policy['id']}/publish", json={}, headers=bearer(token)
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "routing_policy_invalid"
    assert refused.json()["error"]["details"]["errors"][0]["code"] == "weights_sum"


async def test_validation_messages(client: AsyncClient, account) -> None:
    acc, token = account["id"], account["token"]
    policy = await create_policy(
        client, acc, token, "chat-default", document([(account["stable"], 100)])
    )
    stranger = str(uuid.uuid4())
    draft = document(
        [(account["stable"], 100)],
        fallbacks=[account["canary"], account["stable"]],
        rules=[
            {
                "id": "catch-all",
                "match": {},
                "targets": [{"deployment_id": stranger, "weight": 100}],
            },
            {
                "id": "regex",
                "match": {"headers": [{"name": "x-tier", "op": "regex", "value": "("}]},
                "targets": [{"deployment_id": account["canary"], "weight": 100}],
            },
        ],
    )
    report = (
        await client.post(
            f"{policies(acc)}/{policy['id']}/validate", json=draft, headers=bearer(token)
        )
    ).json()
    codes = {issue["code"] for issue in report["errors"]}
    assert {"unknown_deployment", "fallback_cycle", "invalid_regex"} <= codes
    warnings = {issue["code"] for issue in report["warnings"]}
    # chat-canary has no tool parser; nothing is placed, so nothing is ready.
    assert {"tools_unsupported", "no_ready_target", "unreachable_rules"} <= warnings
    tools = [i for i in report["warnings"] if i["code"] == "tools_unsupported"]
    assert any("chat-canary" in issue["message"] for issue in tools)
    # Qwen3.5-4B has a verified tool parser, so it is never named.
    assert not any("chat-stable" in issue["message"] for issue in tools)


async def test_alias_rules(client: AsyncClient, account) -> None:
    acc, token = account["id"], account["token"]
    reserved = await client.post(
        policies(acc),
        json={"alias": "auto", "draft": document([(account["stable"], 100)])},
        headers=bearer(token),
    )
    assert reserved.status_code == 400
    taken = await client.post(
        policies(acc),
        json={"alias": "chat-stable", "draft": document([(account["stable"], 100)])},
        headers=bearer(token),
    )
    assert taken.status_code == 409
    await create_policy(client, acc, token, "chat-default", document([(account["stable"], 100)]))
    duplicate = await client.post(
        policies(acc),
        json={"alias": "chat-default", "draft": document([(account["stable"], 100)])},
        headers=bearer(token),
    )
    assert duplicate.status_code == 409


async def test_simulate_matches_rules_and_reports_the_split(client: AsyncClient, account) -> None:
    acc, token = account["id"], account["token"]
    draft = document(
        [(account["stable"], 90), (account["canary"], 10)],
        fallbacks=[account["big"]],
        rules=[
            {
                "id": "gold",
                "name": "Gold tier",
                "match": {"headers": [{"name": "X-Tier", "op": "equals", "value": "gold"}]},
                "targets": [{"deployment_id": account["big"], "weight": 100}],
            },
            {
                "id": "long",
                "name": "Long prompts",
                "match": {"prompt_tokens": {"min": 8000}},
                "targets": [{"deployment_id": account["big"], "weight": 100}],
            },
        ],
    )
    policy = await create_policy(client, acc, token, "chat-default", draft)
    url = f"{policies(acc)}/{policy['id']}/simulate"

    gold = (
        await client.post(url, json={"request": {"headers": {"x-tier": "gold"}}},
                          headers=bearer(token))
    ).json()
    assert gold["matched_rule_id"] == "gold"
    assert gold["matched_rule_index"] == 1
    assert [t["matched"] for t in gold["trace"]] == [True, False, True]

    default = (await client.post(url, json={"request": {"prompt_tokens": 100}},
                                 headers=bearer(token))).json()
    assert default["is_default"] is True
    assert default["matched_rule_index"] == 0
    shares = {item["name"]: item["percent"] for item in default["distribution"]}
    assert shares == {"chat-stable": 90.0, "chat-canary": 10.0}
    assert default["session_target"] is None
    assert [item["name"] for item in default["fallback_chain"]] == ["chat-big"]

    # A session key always lands on the same target.
    first = (await client.post(url, json={"request": {"session_key": "s-42"}},
                               headers=bearer(token))).json()
    again = (await client.post(url, json={"request": {"session_key": "s-42"}},
                               headers=bearer(token))).json()
    assert first["session_target"]["deployment_id"] == again["session_target"]["deployment_id"]
    assert first["routing_key"] == "session:s-42"

    # Unsaved edits can be simulated inline.
    inline = (
        await client.post(
            url,
            json={"document": document([(account["canary"], 100)]), "request": {}},
            headers=bearer(token),
        )
    ).json()
    assert inline["distribution"][0]["name"] == "chat-canary"

    not_live = await client.post(url, json={"source": "live"}, headers=bearer(token))
    assert not_live.status_code == 400


async def test_simulation_agrees_with_the_enforcement_module(
    client: AsyncClient, account
) -> None:
    """The API's answer equals the data plane's own decision for the same request."""
    fabric_data_plane = data_plane_module()
    acc, token = account["id"], account["token"]
    draft = document(
        [(account["stable"], 70), (account["canary"], 30)],
        rules=[
            {
                "id": "tools",
                "match": {"has_tools": True},
                "targets": [{"deployment_id": account["big"], "weight": 100}],
            }
        ],
    )
    policy = await create_policy(client, acc, token, "chat-default", draft)
    await client.post(f"{policies(acc)}/{policy['id']}/publish", json={}, headers=bearer(token))
    snapshot = {
        "policy_id": policy["id"], "account_id": acc, "alias": "chat-default", "version": 1,
        **draft,
    }
    enforced = fabric_data_plane.Policy.from_document(snapshot)
    for key in ("a", "b", "c", "d", "e", "f"):
        simulated = (
            await client.post(
                f"{policies(acc)}/{policy['id']}/simulate",
                json={"source": "live", "request": {"session_key": key}},
                headers=bearer(token),
            )
        ).json()
        facts = fabric_data_plane.facts_from_request(
            endpoint="chat_completions",
            headers={"X-Fabric-Session": key},
            payload={"messages": []},
            api_key_id=None,
            principal_id=None,
        )
        decision = fabric_data_plane.decide(enforced, facts)
        assert simulated["session_target"]["deployment_id"] == decision.target


async def test_another_account_cannot_read_or_target(client: AsyncClient, account) -> None:
    acc, token = account["id"], account["token"]
    policy = await create_policy(
        client, acc, token, "chat-default", document([(account["stable"], 100)])
    )
    other_id, other_token = await onboard(client, "rp-intruder", "rp-intruder-account")
    reset_context()

    # The other account's token is refused on this account's paths ...
    for method, path in (
        ("GET", policies(acc)),
        ("GET", f"{policies(acc)}/{policy['id']}"),
        ("POST", f"{policies(acc)}/{policy['id']}/simulate"),
    ):
        response = await client.request(method, path, json={}, headers=bearer(other_token))
        assert response.status_code == 403, path
    # ... and the policy does not exist under its own account.
    hidden = await client.get(f"{policies(other_id)}/{policy['id']}", headers=bearer(other_token))
    assert hidden.status_code == 404
    assert (await client.get(policies(other_id), headers=bearer(other_token))).json() == []

    # It cannot publish a policy targeting this account's deployment.
    theirs = await create_policy(
        client, other_id, other_token, "stolen", document([(account["stable"], 100)])
    )
    assert theirs["validation"]["errors"][0]["code"] == "unknown_deployment"
    refused = await client.post(
        f"{policies(other_id)}/{theirs['id']}/publish", json={}, headers=bearer(other_token)
    )
    assert refused.status_code == 400


async def test_viewer_scope(client: AsyncClient, account) -> None:
    """Writes need deployments:write; reads need deployments:read."""
    acc, token = account["id"], account["token"]
    created = await client.post(
        f"/v1/accounts/{acc}/api-keys",
        json={"name": "reader", "scopes": ["deployments:read"]},
        headers=bearer(token),
    )
    assert created.status_code == 201, created.text
    exchanged = await client.post(
        "/v1/token",
        json={"grant_type": "api_key", "api_key": created.json()["secret"],
              "audience": "fabric-control"},
    )
    reader = exchanged.json()["access_token"]
    reset_context()
    assert (await client.get(policies(acc), headers=bearer(reader))).status_code == 200
    denied = await client.post(
        policies(acc), json={"alias": "x", "draft": document([(account["stable"], 100)])},
        headers=bearer(reader),
    )
    assert denied.status_code == 403


async def test_audit_trail(client: AsyncClient, account, db_session: AsyncSession) -> None:
    acc, token = account["id"], account["token"]
    policy = await create_policy(
        client, acc, token, "chat-default", document([(account["stable"], 100)])
    )
    url = f"{policies(acc)}/{policy['id']}"
    await client.post(f"{url}/publish", json={}, headers=bearer(token))
    await client.post(f"{url}/publish", json={}, headers=bearer(token))
    await client.post(f"{url}/rollback", json={"version": 1}, headers=bearer(token))
    deleted = await client.delete(url, headers=bearer(token))
    assert deleted.status_code == 204
    assert (await client.get(url, headers=bearer(token))).status_code == 404
    reset_context()
    with system_context():
        actions = [
            row.action
            for row in (
                await db_session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.resource_type == "routing_policy")
                    .order_by(AuditEvent.created_at)
                )
            ).scalars()
        ]
    assert actions == [
        "routing_policy.created",
        "routing_policy.published",
        "routing_policy.published",
        "routing_policy.rolled_back",
        "routing_policy.deleted",
    ]
    # The alias is free again.
    await create_policy(client, acc, token, "chat-default", document([(account["stable"], 100)]))


async def test_stamps_receive_live_policies_in_desired_state(
    client: AsyncClient, account
) -> None:
    acc, token = account["id"], account["token"]
    enrolled = await enroll_stamp(client, acc, token)
    stamp_id, agent = enrolled["stamp"]["id"], enrolled["agent_credential"]
    placed = await place(client, acc, token, account["stable"], stamp_id=stamp_id)
    assert placed.status_code == 201
    reset_context()

    async def pull() -> dict[str, Any]:
        response = await client.get(
            f"/v1/stamps/{stamp_id}/desired-state?after_generation=999", headers=bearer(agent)
        )
        assert response.status_code == 200, response.text
        reset_context()
        return response.json()["routing_policies"]

    empty = await pull()
    assert empty["policies"] == []

    policy = await create_policy(
        client, acc, token, "chat-default", document([(account["stable"], 100)])
    )
    # A draft is not delivered.
    assert (await pull())["policies"] == []
    await client.post(f"{policies(acc)}/{policy['id']}/publish", json={}, headers=bearer(token))
    reset_context()
    delivered = await pull()
    assert delivered["revision"] != empty["revision"]
    [snapshot] = delivered["policies"]
    assert snapshot["alias"] == "chat-default"
    assert snapshot["version"] == 1
    assert snapshot["account_id"] == acc
    assert snapshot["default"]["targets"] == [{"deployment_id": account["stable"], "weight": 100}]
    # Unchanged policies keep the revision, so the agent writes nothing new.
    assert (await pull())["revision"] == delivered["revision"]

    # Another account's policies never reach this stamp.
    other_id, other_token = await onboard(client, "rp-other", "rp-other-account")
    other_dep = await deployment(client, other_id, other_token, name="their-model")
    theirs = await create_policy(
        client, other_id, other_token, "their-alias", document([(other_dep["id"], 100)])
    )
    await client.post(
        f"{policies(other_id)}/{theirs['id']}/publish", json={}, headers=bearer(other_token)
    )
    reset_context()
    assert [p["alias"] for p in (await pull())["policies"]] == ["chat-default"]

    await client.delete(f"{policies(acc)}/{policy['id']}", headers=bearer(token))
    reset_context()
    assert (await pull())["policies"] == []


async def test_the_edge_table_carries_live_policies(
    client: AsyncClient, account, capsys
) -> None:
    acc, token = account["id"], account["token"]
    enrolled = await enroll_stamp(
        client, acc, token, name="edge-stamp",
        capabilities=capabilities_with_url("https://edge-stamp.example"),
    )
    await place(client, acc, token, account["stable"], stamp_id=enrolled["stamp"]["id"])
    reset_context()
    reader = await edge_token(client, capsys)
    before = await client.get(TABLE, headers=bearer(reader))
    assert before.json()["routing_policies"] == []
    reset_context()

    policy = await create_policy(
        client, acc, token, "chat-default", document([(account["stable"], 100)])
    )
    await client.post(f"{policies(acc)}/{policy['id']}/publish", json={}, headers=bearer(token))
    reset_context()
    after = await client.get(TABLE, headers=bearer(reader))
    body = after.json()
    assert [item["alias"] for item in body["routing_policies"]] == ["chat-default"]
    assert after.headers["etag"] != before.headers["etag"]
    assert body["revision"] >= before.json()["revision"]
