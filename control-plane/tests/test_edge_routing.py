"""The edge routing table (ADR 0021): who can read it and what it contains.

Runs on SQLite by default and, with ``FABRIC_TEST_DATABASE_URL`` set to PostgreSQL, under
row-level security: the table must still see every account's placements there, because
it is read in an explicit elevation, while ordinary account tokens see nothing of it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import _create_edge_credential
from app.core import scopes as scope_defs
from app.core.jwt_service import issue_token
from app.core.tenancy import reset_context, system_context
from app.models import ApiKey, AuditEvent, InferenceStamp
from app.services.accounts import ensure_system_account
from tests.helpers import (
    bearer,
    create_deployment,
    default_capabilities,
    enable_managed_capacity,
    enroll_managed_stamp,
    enroll_stamp,
    onboard,
    place,
)

TABLE = "/v1/edge/routing-table"


async def edge_api_key(capsys) -> str:
    await _create_edge_credential(name="fabric-edge")
    printed = capsys.readouterr().out
    return next(line.split()[1] for line in printed.splitlines() if line.startswith("api_key"))


async def exchange(client: AsyncClient, api_key: str, audience: str = "fabric-control"):
    return await client.post(
        "/v1/token", json={"grant_type": "api_key", "api_key": api_key, "audience": audience}
    )


async def edge_token(client: AsyncClient, capsys) -> str:
    response = await exchange(client, await edge_api_key(capsys))
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == scope_defs.EDGE_ROUTING_READ
    reset_context()
    return response.json()["access_token"]


def capabilities_with_url(url: str | None, region: str = "local") -> dict[str, Any]:
    report = default_capabilities(gpus=8, region=region)
    if url is not None:
        report["inference_url"] = url
    return report


async def serving_stamp(
    client: AsyncClient,
    account_id: str,
    token: str,
    *,
    name: str,
    url: str | None,
    region: str = "local",
    ready: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """An account's BYOI stamp reporting a gateway URL, with one placed deployment."""
    enrolled = await enroll_stamp(
        client, account_id, token, name=name, capabilities=capabilities_with_url(url, region)
    )
    deployment = await create_deployment(client, account_id, token, name=f"{name}-model")
    stamp_id = enrolled["stamp"]["id"]
    placed = await place(client, account_id, token, deployment["id"], stamp_id=stamp_id)
    assert placed.status_code == 201, placed.text
    if ready:
        reported = await client.post(
            f"/v1/stamps/{enrolled['stamp']['id']}/status",
            json={
                "deployment_id": deployment["id"],
                "observed_generation": placed.json()["desired_generation"],
                "phase": "ready",
                "ready_replicas": 1,
            },
            headers=bearer(enrolled["agent_credential"]),
        )
        assert reported.status_code == 200, reported.text
    reset_context()
    return enrolled, deployment


async def test_account_tokens_cannot_read_the_table(client: AsyncClient) -> None:
    _account_id, token = await onboard(client, "edge-owner", "edge-owner-account")
    response = await client.get(TABLE, headers=bearer(token))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"


async def test_the_scope_on_a_customer_token_is_not_enough(client: AsyncClient) -> None:
    """A token naming the scope but bound to a customer account is refused by account."""
    account_id, _token = await onboard(client, "edge-forge", "edge-forge-account")
    forged, _ttl, _exp = issue_token(
        subject=str(uuid.uuid4()),
        account_id=uuid.UUID(account_id),
        principal_type="service",
        scopes=[scope_defs.EDGE_ROUTING_READ],
        audience=scope_defs.AUDIENCE_CONTROL,
        ttl_seconds=300,
    )
    reset_context()
    response = await client.get(TABLE, headers=bearer(forged))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_an_edge_credential"


async def test_an_account_key_cannot_be_given_the_scope(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "edge-key", "edge-key-account")
    response = await client.post(
        f"/v1/accounts/{account_id}/api-keys",
        json={"name": "sneaky", "scopes": [scope_defs.EDGE_ROUTING_READ]},
        headers=bearer(token),
    )
    assert response.status_code == 400, response.text


async def test_the_scope_copied_onto_a_customer_key_is_dropped_at_exchange(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "edge-copy", "edge-copy-account")
    created = await client.post(
        f"/v1/accounts/{account_id}/api-keys",
        json={"name": "reader", "scopes": [scope_defs.DEPLOYMENTS_READ]},
        headers=bearer(token),
    )
    assert created.status_code == 201, created.text
    with system_context():
        await db_session.execute(
            update(ApiKey)
            .where(ApiKey.id == uuid.UUID(created.json()["api_key"]["id"]))
            .values(scopes=[scope_defs.DEPLOYMENTS_READ, scope_defs.EDGE_ROUTING_READ])
        )
        await db_session.commit()
    reset_context()
    exchanged = await exchange(client, created.json()["secret"])
    assert exchanged.status_code == 200, exchanged.text
    assert scope_defs.EDGE_ROUTING_READ not in exchanged.json()["scope"].split()


async def test_the_edge_credential_holds_nothing_else(client: AsyncClient, capsys) -> None:
    api_key = await edge_api_key(capsys)
    inference = await exchange(client, api_key, audience="fabric-inference")
    assert inference.status_code == 403
    control = await exchange(client, api_key)
    token = control.json()["access_token"]
    reset_context()
    # A system-account token with only the edge scope cannot manage anything.
    system_id = control.json()["account_id"]
    listed = await client.get(f"/v1/accounts/{system_id}/deployments", headers=bearer(token))
    assert listed.status_code == 403


async def test_the_table_lists_live_placements_across_accounts(
    client: AsyncClient, db_session: AsyncSession, capsys
) -> None:
    first_id, first_token = await onboard(client, "edge-a", "edge-a-account")
    second_id, second_token = await onboard(client, "edge-b", "edge-b-account")
    stamp_a, deployment_a = await serving_stamp(
        client, first_id, first_token, name="stamp-a", url="https://a.stamps.example.com",
        region="us-east",
    )
    _stamp_b, deployment_b = await serving_stamp(
        client, second_id, second_token, name="stamp-b", url="https://b.stamps.example.com/v1",
        ready=False,
    )

    token = await edge_token(client, capsys)
    response = await client.get(TABLE, headers=bearer(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert response.headers["etag"] == body["etag"]
    assert int(response.headers["x-fabric-routing-revision"]) == body["revision"] > 0

    by_id = {entry["deployment_id"]: entry for entry in body["deployments"]}
    assert set(by_id) == {deployment_a["id"], deployment_b["id"]}
    first = by_id[deployment_a["id"]]
    assert first["account_id"] == first_id
    assert first["model_alias"] == "launch-model"
    assert first["capabilities"]["chat"] is True
    [placement] = first["placements"]
    assert placement["stamp_id"] == stamp_a["stamp"]["id"]
    assert placement["stamp_name"] == "stamp-a"
    assert placement["region"] == "us-east"
    assert placement["inference_url"] == "https://a.stamps.example.com"
    assert placement["ready"] is True
    assert placement["gpus"][0]["count"] == 8
    second = by_id[deployment_b["id"]]
    assert second["account_id"] == second_id
    assert second["placements"][0]["ready"] is False
    assert second["placements"][0]["inference_url"] == "https://b.stamps.example.com/v1"

    # Nothing secret leaves: no credential material, no spec, no in-cluster endpoint.
    text = response.text
    for forbidden in ("fab_", "agent_credential", "desired_spec", "runtime", "secret"):
        assert forbidden not in text

    # Full reads are audited against the system account.
    with system_context():
        events = (
            await db_session.execute(
                select(AuditEvent).where(AuditEvent.action == "edge.routing_table.read")
            )
        ).scalars().all()
    assert len(events) == 1
    assert events[0].event_metadata["deployments"] == 2

    # A conditional read with the current ETag is answered with 304 and not audited.
    reset_context()
    unchanged = await client.get(TABLE, headers={**bearer(token), "If-None-Match": body["etag"]})
    assert unchanged.status_code == 304
    assert unchanged.headers["etag"] == body["etag"]


async def test_unroutable_stamps_are_excluded(
    client: AsyncClient, db_session: AsyncSession, capsys
) -> None:
    account_id, token = await onboard(client, "edge-x", "edge-x-account")
    _live, live_deployment = await serving_stamp(
        client, account_id, token, name="live", url="https://live.stamps.example.com"
    )
    await serving_stamp(client, account_id, token, name="no-url", url=None)
    stale, _ = await serving_stamp(
        client, account_id, token, name="stale", url="https://stale.stamps.example.com"
    )
    revoked, _ = await serving_stamp(
        client, account_id, token, name="revoked", url="https://revoked.stamps.example.com"
    )
    with system_context():
        await db_session.execute(
            update(InferenceStamp)
            .where(InferenceStamp.id == uuid.UUID(stale["stamp"]["id"]))
            .values(last_heartbeat_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=1))
        )
        await db_session.commit()
    reset_context()
    revoke = await client.delete(
        f"/v1/accounts/{account_id}/stamps/{revoked['stamp']['id']}",
        headers=bearer(token),
    )
    assert revoke.status_code in (200, 204), revoke.text

    edge = await edge_token(client, capsys)
    body = (await client.get(TABLE, headers=bearer(edge))).json()
    assert [entry["deployment_id"] for entry in body["deployments"]] == [live_deployment["id"]]


async def test_deleted_deployments_are_excluded_and_change_the_etag(
    client: AsyncClient, capsys
) -> None:
    account_id, token = await onboard(client, "edge-del", "edge-del-account")
    _stamp, deployment = await serving_stamp(
        client, account_id, token, name="s", url="https://s.stamps.example.com"
    )
    edge = await edge_token(client, capsys)
    before = (await client.get(TABLE, headers=bearer(edge))).json()
    assert len(before["deployments"]) == 1

    deleted = await client.delete(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}", headers=bearer(token)
    )
    assert deleted.status_code in (200, 202, 204), deleted.text
    reset_context()
    after = await client.get(TABLE, headers={**bearer(edge), "If-None-Match": before["etag"]})
    assert after.status_code == 200
    assert after.json()["deployments"] == []
    assert after.json()["etag"] != before["etag"]
    assert after.json()["revision"] >= before["revision"]


async def test_managed_stamps_serve_customer_placements(
    client: AsyncClient, db_session: AsyncSession, capsys
) -> None:
    managed, _system = await enroll_managed_stamp(
        client,
        db_session,
        capabilities=capabilities_with_url("https://managed.stamps.example.com"),
    )
    account_id, token = await onboard(client, "edge-mgd", "edge-mgd-account")
    await enable_managed_capacity(db_session, account_id)
    deployment = await create_deployment(client, account_id, token)
    stamp_id = managed["stamp"]["id"]
    placed = await place(client, account_id, token, deployment["id"], stamp_id=stamp_id)
    assert placed.status_code == 201, placed.text
    reset_context()

    edge = await edge_token(client, capsys)
    body = (await client.get(TABLE, headers=bearer(edge))).json()
    [entry] = body["deployments"]
    assert entry["account_id"] == account_id
    assert entry["placements"][0]["stamp_id"] == managed["stamp"]["id"]


async def test_the_system_account_check_holds_without_a_system_account(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A system-account-shaped token before the account exists is refused, not crashed."""
    with system_context():
        system_account = await ensure_system_account(db_session)
        await db_session.commit()
    user_token, _ttl, _exp = issue_token(
        subject=str(uuid.uuid4()),
        account_id=system_account.id,
        principal_type="user",
        scopes=[scope_defs.EDGE_ROUTING_READ],
        audience=scope_defs.AUDIENCE_CONTROL,
        ttl_seconds=300,
    )
    response = await client.get(TABLE, headers=bearer(user_token))
    # A person on the system account is not the edge's machine identity.
    assert response.status_code == 403
