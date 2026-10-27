"""Stamp machine-credential rotation with an overlap window (ADR 0018)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import get_settings
from app.core.errors import Unauthorized
from app.core.security import require_telemetry_credential
from app.models import StampCredential, TelemetryCredential
from tests.helpers import bearer, control_token, enroll_stamp, onboard
from tests.test_stamps import _bearer_request


async def _heartbeat(client: AsyncClient, stamp_id: str, credential: str):
    return await client.post(
        f"/v1/stamps/{stamp_id}/heartbeat", json={}, headers=bearer(credential)
    )


async def _rotate(client: AsyncClient, stamp_id: str, credential: str):
    return await client.post(
        f"/v1/stamps/{stamp_id}/credentials/rotate", headers=bearer(credential)
    )


async def _telemetry_ok(session: AsyncSession, credential: str) -> None:
    """Authenticate as the collector, then end the transaction the lookup's write opened."""
    try:
        await require_telemetry_credential(_bearer_request(credential), session)
    finally:
        await session.rollback()


def _later(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Move the credential check's clock forward."""
    shifted = dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=seconds)
    monkeypatch.setattr(security, "utc_now", lambda: shifted)


async def test_rotation_issues_new_credentials_and_overlaps_the_old(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id, token = await onboard(client, "rotate-owner", "rotate-account")
    enrolled = await enroll_stamp(client, account_id, token)
    stamp_id = enrolled["stamp"]["id"]
    old_agent, old_telemetry = enrolled["agent_credential"], enrolled["telemetry_credential"]

    rotated = await _rotate(client, stamp_id, old_agent)
    assert rotated.status_code == 200, rotated.text
    body = rotated.json()
    assert body["credential_version"] == 2
    new_agent, new_telemetry = body["agent_credential"], body["telemetry_credential"]
    assert {new_agent, new_telemetry}.isdisjoint({old_agent, old_telemetry})
    grace = get_settings().stamp_credential_rotation_grace_seconds
    expires = dt.datetime.fromisoformat(body["previous_credentials_expire_at"])
    assert abs((expires - dt.datetime.now(tz=dt.UTC)).total_seconds() - grace) < 60

    # Both generations work during the overlap, for the agent and for the collector.
    assert (await _heartbeat(client, stamp_id, old_agent)).status_code == 200
    assert (await _heartbeat(client, stamp_id, new_agent)).status_code == 200
    await _telemetry_ok(db_session, old_telemetry)
    await _telemetry_ok(db_session, new_telemetry)

    # After it, only the new ones do.
    _later(monkeypatch, grace + 60)
    expired = await _heartbeat(client, stamp_id, old_agent)
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "credential_expired"
    assert (await _heartbeat(client, stamp_id, new_agent)).status_code == 200
    with pytest.raises(Unauthorized) as stale:
        await _telemetry_ok(db_session, old_telemetry)
    assert stale.value.code == "credential_expired"
    await _telemetry_ok(db_session, new_telemetry)


async def test_rotating_again_after_a_lost_reply_retires_the_unheld_credentials(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "rotate-lost", "rotate-lost")
    enrolled = await enroll_stamp(client, account_id, token)
    stamp_id = enrolled["stamp"]["id"]
    old_agent = enrolled["agent_credential"]

    lost = (await _rotate(client, stamp_id, old_agent)).json()
    # The agent never saw that reply, so it rotates again with what it still holds.
    retried = await _rotate(client, stamp_id, old_agent)
    assert retried.status_code == 200
    assert retried.json()["credential_version"] == 3

    rows = (
        await db_session.execute(
            select(StampCredential).where(StampCredential.stamp_id == uuid.UUID(stamp_id))
        )
    ).scalars().all()
    by_version = {row.version: row for row in rows}
    # Only the credential the agent now holds is open-ended; the one in the lost reply
    # expires with the rest instead of staying valid for a holder that does not exist.
    assert by_version[3].expires_at is None
    assert by_version[2].expires_at is not None
    assert by_version[1].expires_at is not None
    assert lost["credential_version"] == 2


async def test_the_telemetry_credential_cannot_rotate(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "rotate-tele", "rotate-tele")
    enrolled = await enroll_stamp(client, account_id, token)
    refused = await _rotate(client, enrolled["stamp"]["id"], enrolled["telemetry_credential"])
    assert refused.status_code == 401


async def test_an_agent_cannot_rotate_another_stamp(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "rotate-other", "rotate-other")
    first = await enroll_stamp(client, account_id, token, name="first")
    second = await enroll_stamp(client, account_id, token, name="second")
    refused = await _rotate(client, second["stamp"]["id"], first["agent_credential"])
    assert refused.status_code == 403


async def test_a_revoked_stamp_cannot_rotate(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "rotate-revoked", "rotate-revoked")
    enrolled = await enroll_stamp(client, account_id, token)
    stamp_id = enrolled["stamp"]["id"]
    await client.delete(f"/v1/accounts/{account_id}/stamps/{stamp_id}", headers=bearer(token))
    refused = await _rotate(client, stamp_id, enrolled["agent_credential"])
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "credential_revoked"


async def test_an_operator_request_reaches_the_agent_through_its_heartbeat(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "rotate-request", "rotate-request")
    enrolled = await enroll_stamp(client, account_id, token)
    stamp_id = enrolled["stamp"]["id"]
    agent = enrolled["agent_credential"]
    assert (await _heartbeat(client, stamp_id, agent)).json()["rotate_credentials"] is False

    requested = await client.post(
        f"/v1/accounts/{account_id}/stamps/{stamp_id}/credential-rotation",
        headers=bearer(token),
    )
    assert requested.status_code == 202, requested.text
    assert requested.json()["credential_rotation_requested_at"] is not None
    assert (await _heartbeat(client, stamp_id, agent)).json()["rotate_credentials"] is True

    rotated = (await _rotate(client, stamp_id, agent)).json()
    heartbeat = await _heartbeat(client, stamp_id, rotated["agent_credential"])
    assert heartbeat.json()["rotate_credentials"] is False

    stamps = await client.get(f"/v1/accounts/{account_id}/stamps", headers=bearer(token))
    stamp = stamps.json()[0]
    assert stamp["credential_version"] == 2
    assert stamp["credential_rotation_requested_at"] is None
    assert stamp["credentials_rotated_at"] is not None

    trail = await client.get(
        f"/v1/accounts/{account_id}/audit-events",
        params={"resource_id": stamp_id},
        headers=bearer(token),
    )
    actions = [event["action"] for event in trail.json()["events"]]
    assert actions[:2] == ["stamp.credentials_rotated", "stamp.credential_rotation_requested"]
    rotation = trail.json()["events"][0]
    assert rotation["actor_type"] == "stamp"
    assert rotation["metadata"]["requested"] is True
    assert rotation["metadata"]["credential_version"] == 2


async def test_requesting_rotation_needs_stamps_write(client: AsyncClient) -> None:
    account_id, owner_token = await onboard(client, "rotate-boss", "rotate-roles")
    enrolled = await enroll_stamp(client, account_id, owner_token)
    added = await client.post(
        f"/v1/accounts/{account_id}/members",
        json={"auth0_subject": "rotate-viewer", "role": "viewer"},
        headers=bearer(owner_token),
    )
    assert added.status_code == 201, added.text
    viewer = await control_token(client, "rotate-viewer", account_id)
    refused = await client.post(
        f"/v1/accounts/{account_id}/stamps/{enrolled['stamp']['id']}/credential-rotation",
        headers=bearer(viewer),
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "insufficient_scope"


async def test_new_credentials_carry_the_stamps_new_version(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "rotate-version", "rotate-version")
    enrolled = await enroll_stamp(client, account_id, token)
    stamp_id = enrolled["stamp"]["id"]
    await _rotate(client, stamp_id, enrolled["agent_credential"])
    for model in (StampCredential, TelemetryCredential):
        versions = sorted(
            (
                await db_session.execute(
                    select(model.version).where(model.stamp_id == uuid.UUID(stamp_id))
                )
            ).scalars()
        )
        assert versions == [1, 2]


async def test_a_superseded_credential_is_told_to_rotate(client: AsyncClient) -> None:
    """Recovers a rotation whose reply was lost or could not be saved, however it started."""
    account_id, token = await onboard(client, "rotate-told", "rotate-told")
    enrolled = await enroll_stamp(client, account_id, token)
    stamp_id, old_agent = enrolled["stamp"]["id"], enrolled["agent_credential"]
    assert (await _heartbeat(client, stamp_id, old_agent)).json()["rotate_credentials"] is False

    await _rotate(client, stamp_id, old_agent)  # the agent never saw this reply
    assert (await _heartbeat(client, stamp_id, old_agent)).json()["rotate_credentials"] is True


async def test_a_superseded_credential_cannot_rotate_once_the_newer_one_is_used(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "rotate-stale", "rotate-stale")
    enrolled = await enroll_stamp(client, account_id, token)
    stamp_id, old_agent = enrolled["stamp"]["id"], enrolled["agent_credential"]
    new_agent = (await _rotate(client, stamp_id, old_agent)).json()["agent_credential"]
    assert (await _heartbeat(client, stamp_id, new_agent)).status_code == 200

    # A copy of the old credential cannot mint a fresh pair or cut the live one short.
    refused = await _rotate(client, stamp_id, old_agent)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "credential_superseded"
    live = (
        await db_session.execute(
            select(StampCredential).where(
                StampCredential.stamp_id == uuid.UUID(stamp_id), StampCredential.version == 2
            )
        )
    ).scalar_one()
    assert live.expires_at is None
    # The agent holding the newest credential can still rotate.
    assert (await _rotate(client, stamp_id, new_agent)).status_code == 200
