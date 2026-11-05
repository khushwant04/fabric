"""Reading and exporting an account's audit trail."""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

from app.api.v1 import audit as audit_api
from tests.helpers import bearer, control_token, onboard


async def _principal(client: AsyncClient, account_id: str, token: str, name: str) -> str:
    response = await client.post(
        f"/v1/accounts/{account_id}/service-principals",
        json={"name": name},
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _events(client: AsyncClient, account_id: str, token: str, **params) -> dict:
    response = await client.get(
        f"/v1/accounts/{account_id}/audit-events", params=params, headers=bearer(token)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_owner_reads_the_trail_newest_first(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "audit-owner", "audit-account")
    principal_id = await _principal(client, account_id, token, "ci-runner")

    page = await _events(client, account_id, token)
    actions = [event["action"] for event in page["events"]]
    assert actions[0] == "service_principal.created"
    assert "account.created" in actions
    assert page["next_cursor"] is None

    newest = page["events"][0]
    assert newest["account_id"] == account_id
    assert newest["resource_type"] == "service_principal"
    assert newest["resource_id"] == principal_id
    assert newest["actor_type"] == "user"
    timestamps = [event["created_at"] for event in page["events"]]
    assert timestamps == sorted(timestamps, reverse=True)


async def test_pages_cover_the_trail_exactly_once(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "audit-pager", "audit-pager")
    for index in range(7):
        await _principal(client, account_id, token, f"principal-{index}")
    everything = [event["id"] for event in (await _events(client, account_id, token))["events"]]
    assert len(everything) >= 8

    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 3} | ({"cursor": cursor} if cursor else {})
        page = await _events(client, account_id, token, **params)
        assert len(page["events"]) <= 3
        seen.extend(event["id"] for event in page["events"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == everything


async def test_filters_narrow_the_trail(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "audit-filter", "audit-filter")
    first = await _principal(client, account_id, token, "first")
    await _principal(client, account_id, token, "second")

    by_action = await _events(client, account_id, token, action="service_principal.created")
    assert len(by_action["events"]) == 2
    by_resource = await _events(client, account_id, token, resource_id=first)
    assert [event["resource_id"] for event in by_resource["events"]] == [first]

    newest = by_action["events"][0]["created_at"]
    before = await _events(
        client, account_id, token, action="service_principal.created", until=newest
    )
    assert [event["resource_id"] for event in before["events"]] == [first]
    since = await _events(
        client, account_id, token, action="service_principal.created", since=newest
    )
    assert len(since["events"]) == 1


async def test_reading_the_trail_needs_audit_read(client: AsyncClient) -> None:
    account_id, owner_token = await onboard(client, "audit-boss", "audit-roles")
    added = await client.post(
        f"/v1/accounts/{account_id}/members",
        json={"auth0_subject": "audit-dev", "role": "developer"},
        headers=bearer(owner_token),
    )
    assert added.status_code == 201, added.text
    developer_token = await control_token(client, "audit-dev", account_id)

    refused = await client.get(
        f"/v1/accounts/{account_id}/audit-events", headers=bearer(developer_token)
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "insufficient_scope"

    # Nor can a developer mint a key that reads it on their behalf.
    escalated = await client.post(
        f"/v1/accounts/{account_id}/api-keys",
        json={"name": "audit", "scopes": ["audit:read"]},
        headers=bearer(developer_token),
    )
    assert escalated.status_code == 403
    assert escalated.json()["error"]["details"]["scopes"] == ["audit:read"]


async def test_an_owner_can_delegate_audit_read_to_a_key(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "audit-siem", "audit-siem")
    created = await client.post(
        f"/v1/accounts/{account_id}/api-keys",
        json={"name": "siem-export", "scopes": ["audit:read"]},
        headers=bearer(token),
    )
    assert created.status_code == 201, created.text
    exchanged = await client.post(
        "/v1/token", json={"grant_type": "api_key", "api_key": created.json()["secret"]}
    )
    assert exchanged.status_code == 200, exchanged.text
    page = await _events(client, account_id, exchanged.json()["access_token"])
    assert page["events"][0]["action"] == "api_key.created"


async def test_one_account_never_sees_another_accounts_trail(client: AsyncClient) -> None:
    account_a, token_a = await onboard(client, "audit-a", "audit-tenant-a")
    account_b, token_b = await onboard(client, "audit-b", "audit-tenant-b")
    principal_a = await _principal(client, account_a, token_a, "only-in-a")

    crossed = await client.get(
        f"/v1/accounts/{account_a}/audit-events", headers=bearer(token_b)
    )
    assert crossed.status_code == 403
    assert crossed.json()["error"]["code"] == "account_mismatch"

    own = await _events(client, account_b, token_b)
    assert all(event["account_id"] == account_b for event in own["events"])
    assert principal_a not in {event["resource_id"] for event in own["events"]}


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"cursor": "not-a-cursor"}, "invalid_cursor"),
        ({"since": "2026-10-01T00:00:00"}, "invalid_time_bound"),
        (
            {"since": "2026-10-02T00:00:00Z", "until": "2026-10-01T00:00:00Z"},
            "invalid_time_range",
        ),
    ],
)
async def test_malformed_queries_are_client_errors(
    client: AsyncClient, params: dict, code: str
) -> None:
    slug = "audit-bad-" + code.replace("_", "-")
    account_id, token = await onboard(client, slug, slug)
    response = await client.get(
        f"/v1/accounts/{account_id}/audit-events", params=params, headers=bearer(token)
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code


async def test_export_is_ndjson_and_is_itself_audited(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "audit-export", "audit-export")
    await _principal(client, account_id, token, "exported")

    response = await client.get(
        f"/v1/accounts/{account_id}/audit-events/export",
        params={"action": "service_principal.created"},
        headers=bearer(token),
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert audit_api.NEXT_CURSOR_HEADER not in response.headers
    lines = [json.loads(line) for line in response.text.splitlines()]
    assert [line["action"] for line in lines] == ["service_principal.created"]

    recorded = await _events(client, account_id, token, action="audit.exported")
    assert len(recorded["events"]) == 1
    metadata = recorded["events"][0]["metadata"]
    assert metadata == {
        "filters": {"action": "service_principal.created"},
        "events": 1,
        "continued": False,
    }


async def test_export_continues_from_its_cursor(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit_api, "EXPORT_PAGE_LIMIT", 2)
    account_id, token = await onboard(client, "audit-long", "audit-long")
    for index in range(3):
        await _principal(client, account_id, token, f"long-{index}")
    params = {"action": "service_principal.created"}

    first = await client.get(
        f"/v1/accounts/{account_id}/audit-events/export", params=params, headers=bearer(token)
    )
    cursor = first.headers[audit_api.NEXT_CURSOR_HEADER]
    second = await client.get(
        f"/v1/accounts/{account_id}/audit-events/export",
        params=params | {"cursor": cursor},
        headers=bearer(token),
    )
    assert audit_api.NEXT_CURSOR_HEADER not in second.headers
    ids = [json.loads(line)["id"] for line in (first.text + second.text).splitlines()]
    assert len(ids) == len(set(ids)) == 3
