"""Stamp enrollment, credential separation, placement rules, and sync loop."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.errors import Unauthorized
from app.core.security import require_agent_credential, require_telemetry_credential
from app.core.timeutil import utc_now
from app.models import Account, InferenceStamp
from app.services.accounts import ensure_system_account
from app.services.stamps import create_enrollment_token
from tests.helpers import bearer, create_deployment, enroll_stamp, onboard

CAPABILITIES = {
    "orchestrator": "k3s",
    "region": "local",
    "gpus": [{"product": "test-gpu", "count": 2, "memory_bytes": 2048}],
    "allocatable_gpus": 2,
    "requested_gpus": 1,
}


def _bearer_request(token: str) -> Request:
    """Minimal ASGI request carrying a machine credential.

    Used to exercise the credential dependencies directly, including the
    collector dependency that has no route until telemetry ingestion exists.
    """
    return Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )


async def test_enrollment_issues_distinct_agent_and_telemetry_credentials(
    client: AsyncClient,
) -> None:
    account_id, token = await onboard(client, "stamp-user", "stamp-account")
    enrolled = await enroll_stamp(client, account_id, token)

    agent = enrolled["agent_credential"]
    telemetry = enrolled["telemetry_credential"]
    assert agent != telemetry
    assert agent.startswith("fab_agent_")
    assert telemetry.startswith("fab_telem_")
    assert enrolled["stamp"]["account_id"] == account_id
    assert enrolled["stamp"]["mode"] == "byoi"
    assert enrolled["stamp"]["orchestrator"] == "k3s"

    stamp_id = enrolled["stamp"]["id"]
    # The agent credential works on agent endpoints.
    heartbeat = await client.post(
        f"/v1/stamps/{stamp_id}/heartbeat", json={}, headers=bearer(agent)
    )
    assert heartbeat.status_code == 200

    # The telemetry credential must not be accepted for agent operations.
    denied = await client.post(
        f"/v1/stamps/{stamp_id}/heartbeat", json={}, headers=bearer(telemetry)
    )
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "invalid_credential"


async def test_enrollment_token_is_single_use(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "reuse-user", "reuse-account")
    created = await client.post(
        f"/v1/accounts/{account_id}/stamp-enrollment-tokens",
        json={"allowed_mode": "byoi"},
        headers=bearer(token),
    )
    enrollment_token = created.json()["enrollment_token"]
    payload = {
        "enrollment_token": enrollment_token,
        "name": "cluster",
        "capabilities": CAPABILITIES,
    }
    first = await client.post("/v1/stamps/enroll", json=payload)
    assert first.status_code == 201

    second = await client.post("/v1/stamps/enroll", json=payload)
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "enrollment_token_used"


async def test_enrollment_token_cannot_be_claimed_concurrently(client: AsyncClient) -> None:
    """Single use must hold under concurrency, not only sequentially."""
    account_id, token = await onboard(client, "race-user", "race-account")
    created = await client.post(
        f"/v1/accounts/{account_id}/stamp-enrollment-tokens",
        json={"allowed_mode": "byoi"},
        headers=bearer(token),
    )
    payload = {
        "enrollment_token": created.json()["enrollment_token"],
        "name": "cluster",
        "capabilities": CAPABILITIES,
    }
    responses = await asyncio.gather(
        *(client.post("/v1/stamps/enroll", json=payload) for _ in range(4))
    )
    codes = sorted(response.status_code for response in responses)
    assert codes == [201, 401, 401, 401], codes
    for response in responses:
        if response.status_code == 401:
            assert response.json()["error"]["code"] == "enrollment_token_used"

    # Exactly one stamp exists for the account.
    listed = await client.get(f"/v1/accounts/{account_id}/stamps", headers=bearer(token))
    assert len(listed.json()) == 1


async def test_revoked_enrollment_token_cannot_register_a_stamp(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "etok-revoke", "etok-revoke-account")
    created = await client.post(
        f"/v1/accounts/{account_id}/stamp-enrollment-tokens",
        json={"allowed_mode": "byoi"},
        headers=bearer(token),
    )
    body = created.json()
    revoked = await client.delete(
        f"/v1/accounts/{account_id}/stamp-enrollment-tokens/{body['id']}",
        headers=bearer(token),
    )
    assert revoked.status_code == 204

    response = await client.post(
        "/v1/stamps/enroll",
        json={
            "enrollment_token": body["enrollment_token"],
            "name": "cluster",
            "capabilities": CAPABILITIES,
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "enrollment_token_revoked"


async def test_managed_enrollment_requires_system_account(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "managed-user", "managed-account")
    response = await client.post(
        f"/v1/accounts/{account_id}/stamp-enrollment-tokens",
        json={"allowed_mode": "managed"},
        headers=bearer(token),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "managed_enrollment_not_permitted"


async def test_capability_report_rejects_unknown_fields(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "caps-user", "caps-account")
    created = await client.post(
        f"/v1/accounts/{account_id}/stamp-enrollment-tokens",
        json={"allowed_mode": "byoi"},
        headers=bearer(token),
    )
    response = await client.post(
        "/v1/stamps/enroll",
        json={
            "enrollment_token": created.json()["enrollment_token"],
            "name": "cluster",
            "capabilities": {**CAPABILITIES, "kubeconfig": "secret-material"},
        },
    )
    assert response.status_code == 422


async def test_placement_and_desired_state_round_trip(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "sync-user", "sync-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    stamp_id = enrolled["stamp"]["id"]
    agent = enrolled["agent_credential"]

    placed = await client.post(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/placements",
        json={"stamp_id": stamp_id},
        headers=bearer(token),
    )
    assert placed.status_code == 201
    assert placed.json()["desired_generation"] == 1

    desired = await client.get(
        f"/v1/stamps/{stamp_id}/desired-state?after_generation=0", headers=bearer(agent)
    )
    assert desired.status_code == 200
    body = desired.json()
    assert body["max_generation"] == 1
    assert len(body["deployments"]) == 1
    assert body["deployments"][0]["deployment_id"] == deployment["id"]
    assert body["deployments"][0]["deleted"] is False

    # Acknowledged generations are not resent.
    empty = await client.get(
        f"/v1/stamps/{stamp_id}/desired-state?after_generation=1", headers=bearer(agent)
    )
    assert empty.json()["deployments"] == []

    reported = await client.post(
        f"/v1/stamps/{stamp_id}/status",
        json={
            "deployment_id": deployment["id"],
            "observed_generation": 1,
            "phase": "ready",
            "ready_replicas": 1,
            "endpoint": "https://stamp.local/v1",
        },
        headers=bearer(agent),
    )
    assert reported.status_code == 200

    status_view = await client.get(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/status",
        headers=bearer(token),
    )
    assert status_view.status_code == 200
    entry = status_view.json()[0]
    assert entry["phase"] == "ready"
    assert entry["ready_replicas"] == 1
    assert entry["observed_generation"] == 1
    assert entry["endpoint"] == "https://stamp.local/v1"

    # Updating intent re-advertises the deployment to the stamp.
    await client.patch(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}",
        json={"spec": {"runtime": {"release": "runtime-release-2"}, "replicas": 2}},
        headers=bearer(token),
    )
    resent = await client.get(
        f"/v1/stamps/{stamp_id}/desired-state?after_generation=1", headers=bearer(agent)
    )
    assert resent.json()["deployments"][0]["desired_generation"] == 2


async def test_agent_cannot_use_another_stamp_path(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "path-user", "path-account")
    first = await enroll_stamp(client, account_id, token, name="one")
    second = await enroll_stamp(client, account_id, token, name="two")

    response = await client.post(
        f"/v1/stamps/{second['stamp']['id']}/heartbeat",
        json={},
        headers=bearer(first["agent_credential"]),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "stamp_mismatch"


async def test_cross_account_placement_is_rejected(client: AsyncClient) -> None:
    account_a, token_a = await onboard(client, "place-a", "place-a-account")
    account_b, token_b = await onboard(client, "place-b", "place-b-account")

    stamp_b = await enroll_stamp(client, account_b, token_b, name="b-cluster")
    deployment_a = await create_deployment(client, account_a, token_a)

    response = await client.post(
        f"/v1/accounts/{account_a}/deployments/{deployment_a['id']}/placements",
        json={"stamp_id": stamp_b["stamp"]["id"]},
        headers=bearer(token_a),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "stamp_not_available"


async def test_status_for_unplaced_deployment_is_rejected(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "unplaced-user", "unplaced-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)

    response = await client.post(
        f"/v1/stamps/{enrolled['stamp']['id']}/status",
        json={"deployment_id": deployment["id"], "phase": "ready"},
        headers=bearer(enrolled["agent_credential"]),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "placement_not_found"


async def _enroll_managed_stamp(
    client: AsyncClient, db_session: AsyncSession, *, orchestrator: str = "aks"
) -> tuple[dict, Account]:
    """Register a managed stamp owned by the protected Fabric system account."""
    system_account = await ensure_system_account(db_session)
    _record, enrollment_token = await create_enrollment_token(
        db_session,
        account=system_account,
        allowed_mode="managed",
        expires_in_minutes=30,
        actor_user_id=None,
    )
    await db_session.commit()

    enrolled = await client.post(
        "/v1/stamps/enroll",
        json={
            "enrollment_token": enrollment_token,
            "name": f"managed-{orchestrator}",
            "capabilities": {**CAPABILITIES, "orchestrator": orchestrator},
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    return enrolled.json(), system_account


async def test_managed_placement_requires_entitlement(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "entitle-user", "entitle-account")
    deployment = await create_deployment(client, account_id, token)

    enrolled, system_account = await _enroll_managed_stamp(client, db_session)
    managed_stamp_id = enrolled["stamp"]["id"]
    assert enrolled["stamp"]["mode"] == "managed"
    assert enrolled["stamp"]["account_id"] == str(system_account.id)

    denied = await client.post(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/placements",
        json={"stamp_id": managed_stamp_id},
        headers=bearer(token),
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "managed_capacity_not_enabled"

    await db_session.execute(
        update(Account)
        .where(Account.id == uuid.UUID(account_id))
        .values(managed_capacity_enabled=True)
    )
    await db_session.commit()

    allowed = await client.post(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/placements",
        json={"stamp_id": managed_stamp_id},
        headers=bearer(token),
    )
    assert allowed.status_code == 201
    # The placement remains owned by the customer account.
    assert allowed.json()["account_id"] == account_id
    assert allowed.json()["stamp_id"] == managed_stamp_id


async def test_managed_placement_requires_supported_orchestrator(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "orch-user", "orch-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled, _system = await _enroll_managed_stamp(client, db_session, orchestrator="nomad")

    await db_session.execute(
        update(Account)
        .where(Account.id == uuid.UUID(account_id))
        .values(managed_capacity_enabled=True)
    )
    await db_session.commit()

    response = await client.post(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/placements",
        json={"stamp_id": enrolled["stamp"]["id"]},
        headers=bearer(token),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "unsupported_orchestrator"


async def test_managed_stamp_reports_status_for_customer_deployment(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A system-account stamp reports status for a customer-owned placement."""
    account_id, token = await onboard(client, "mgd-status", "mgd-status-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled, system_account = await _enroll_managed_stamp(client, db_session)
    stamp_id = enrolled["stamp"]["id"]
    agent = enrolled["agent_credential"]

    await db_session.execute(
        update(Account)
        .where(Account.id == uuid.UUID(account_id))
        .values(managed_capacity_enabled=True)
    )
    await db_session.commit()

    placed = await client.post(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/placements",
        json={"stamp_id": stamp_id},
        headers=bearer(token),
    )
    assert placed.status_code == 201

    desired = await client.get(
        f"/v1/stamps/{stamp_id}/desired-state", headers=bearer(agent)
    )
    assert [item["deployment_id"] for item in desired.json()["deployments"]] == [
        deployment["id"]
    ]

    reported = await client.post(
        f"/v1/stamps/{stamp_id}/status",
        json={
            "deployment_id": deployment["id"],
            "observed_generation": 1,
            "phase": "ready",
            "ready_replicas": 1,
            "conditions": [{"type": "Available", "status": "True", "reason": "MinimumReplicas"}],
        },
        headers=bearer(agent),
    )
    assert reported.status_code == 200, reported.text

    # The customer, not the system account, can read the reported status.
    customer_view = await client.get(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/status",
        headers=bearer(token),
    )
    entry = customer_view.json()[0]
    assert entry["phase"] == "ready"
    assert entry["stamp_id"] == stamp_id
    assert entry["conditions"][0]["type"] == "Available"


async def test_status_report_rejects_unbounded_conditions(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "cond-user", "cond-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    await client.post(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/placements",
        json={"stamp_id": enrolled["stamp"]["id"]},
        headers=bearer(token),
    )

    response = await client.post(
        f"/v1/stamps/{enrolled['stamp']['id']}/status",
        json={
            "deployment_id": deployment["id"],
            "phase": "ready",
            "conditions": [{"type": "Available", "status": "True", "kubeconfig": "secret"}],
        },
        headers=bearer(enrolled["agent_credential"]),
    )
    assert response.status_code == 422


async def test_revoked_stamp_cannot_write_status(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Stamp revocation stops status writes even if a credential still verifies."""
    account_id, token = await onboard(client, "rev-status", "rev-status-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    stamp_id = enrolled["stamp"]["id"]
    agent = enrolled["agent_credential"]
    await client.post(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/placements",
        json={"stamp_id": stamp_id},
        headers=bearer(token),
    )

    await db_session.execute(
        update(InferenceStamp)
        .where(InferenceStamp.id == uuid.UUID(stamp_id))
        .values(revoked_at=utc_now())
    )
    await db_session.commit()

    for call in (
        client.post(f"/v1/stamps/{stamp_id}/heartbeat", json={}, headers=bearer(agent)),
        client.get(f"/v1/stamps/{stamp_id}/desired-state", headers=bearer(agent)),
        client.post(
            f"/v1/stamps/{stamp_id}/status",
            json={"deployment_id": deployment["id"], "phase": "ready"},
            headers=bearer(agent),
        ),
    ):
        response = await call
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "stamp_revoked"


async def test_stamp_revocation_endpoint_revokes_both_credentials(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "rev-endpoint", "rev-endpoint-account")
    enrolled = await enroll_stamp(client, account_id, token)
    stamp_id = enrolled["stamp"]["id"]
    agent = enrolled["agent_credential"]
    telemetry = enrolled["telemetry_credential"]

    revoked = await client.delete(
        f"/v1/accounts/{account_id}/stamps/{stamp_id}", headers=bearer(token)
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert revoked.json()["status"] == "revoked"

    denied = await client.post(
        f"/v1/stamps/{stamp_id}/heartbeat", json={}, headers=bearer(agent)
    )
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "credential_revoked"

    with pytest.raises(Unauthorized) as revoked_telemetry:
        await require_telemetry_credential(_bearer_request(telemetry), db_session)
    assert revoked_telemetry.value.code == "credential_revoked"


async def test_credential_classes_are_not_interchangeable(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The collector credential authenticates only as a collector, and vice versa."""
    account_id, token = await onboard(client, "class-user", "class-account")
    enrolled = await enroll_stamp(client, account_id, token)
    agent = enrolled["agent_credential"]
    telemetry = enrolled["telemetry_credential"]

    context = await require_telemetry_credential(_bearer_request(telemetry), db_session)
    assert str(context.stamp_id) == enrolled["stamp"]["id"]
    assert context.scopes == frozenset({"telemetry:write"})

    with pytest.raises(Unauthorized) as agent_as_collector:
        await require_telemetry_credential(_bearer_request(agent), db_session)
    assert agent_as_collector.value.code == "invalid_credential"

    with pytest.raises(Unauthorized) as collector_as_agent:
        await require_agent_credential(_bearer_request(telemetry), db_session)
    assert collector_as_agent.value.code == "invalid_credential"
