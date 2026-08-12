"""Auth0 exchange, account selection, and issued-token contents."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.jwt_service import decode_token
from tests.helpers import create_account, exchange_auth0, onboard, user_headers


async def test_zero_memberships_is_rejected_without_token(client: AsyncClient) -> None:
    response = await exchange_auth0(client, "newcomer")
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "account_membership_required"
    assert "access_token" not in body


async def test_single_membership_is_selected_implicitly(client: AsyncClient) -> None:
    account_id = await create_account(client, "solo", "solo-account")
    response = await exchange_auth0(client, "solo")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account_id"] == account_id
    assert body["token_type"] == "Bearer"

    claims = decode_token(body["access_token"], audience="fabric-control")
    assert claims["account_id"] == account_id
    assert claims["principal_type"] == "user"
    # Owner role grants the full control scope set.
    assert "deployments:write" in claims["scp"]
    assert "members:write" in claims["scp"]


async def test_multiple_memberships_require_explicit_selection(client: AsyncClient) -> None:
    first = await create_account(client, "multi", "multi-one")
    second = await create_account(client, "multi", "multi-two")

    ambiguous = await exchange_auth0(client, "multi")
    assert ambiguous.status_code == 400
    body = ambiguous.json()
    assert body["error"]["code"] == "account_selection_required"
    assert set(body["error"]["details"]["accounts"]) == {first, second}
    assert "access_token" not in body

    selected = await exchange_auth0(client, "multi", account_id=second)
    assert selected.status_code == 200
    assert selected.json()["account_id"] == second


async def test_foreign_account_selection_is_rejected(client: AsyncClient) -> None:
    other_account = await create_account(client, "owner-a", "owner-a-account")
    await create_account(client, "owner-b", "owner-b-account")

    response = await exchange_auth0(client, "owner-b", account_id=other_account)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_account_selection"


async def test_inference_audience_grants_only_invoke_scope(client: AsyncClient) -> None:
    account_id = await create_account(client, "inference-user", "inference-account")
    response = await exchange_auth0(
        client, "inference-user", account_id=account_id, audience="fabric-inference"
    )
    assert response.status_code == 200
    claims = decode_token(response.json()["access_token"], audience="fabric-inference")
    assert claims["scp"] == ["inference:invoke"]


async def test_me_reports_user_and_memberships(client: AsyncClient) -> None:
    account_id, _token = await onboard(client, "me-user", "me-account")
    response = await client.get("/v1/me", headers=user_headers("me-user"))
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["auth0_subject"] == "me-user"
    assert [membership["account_id"] for membership in body["memberships"]] == [account_id]
    assert body["memberships"][0]["role"] == "owner"


async def test_invalid_auth0_assertion_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/token",
        json={"grant_type": "auth0_token", "assertion": "not-a-user-token"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_assertion"
