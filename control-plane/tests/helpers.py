"""HTTP helpers shared by tests."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient, Response


def user_headers(subject: str) -> dict[str, str]:
    """Headers representing an Auth0-authenticated human."""
    return {"Authorization": f"Bearer user:{subject}"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def create_account(client: AsyncClient, subject: str, slug: str) -> str:
    response = await client.post(
        "/v1/accounts",
        json={"slug": slug, "name": slug.replace("-", " ").title()},
        headers=user_headers(subject),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def exchange_auth0(
    client: AsyncClient,
    subject: str,
    *,
    account_id: str | None = None,
    audience: str = "fabric-control",
) -> Response:
    payload: dict[str, Any] = {
        "grant_type": "auth0_token",
        "assertion": f"user:{subject}",
        "audience": audience,
    }
    if account_id is not None:
        payload["account_id"] = account_id
    return await client.post("/v1/token", json=payload)


async def control_token(client: AsyncClient, subject: str, account_id: str | None = None) -> str:
    response = await exchange_auth0(client, subject, account_id=account_id)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def onboard(client: AsyncClient, subject: str, slug: str) -> tuple[str, str]:
    """Create an account for a new user and return ``(account_id, control_token)``."""
    account_id = await create_account(client, subject, slug)
    return account_id, await control_token(client, subject, account_id)


async def create_deployment(
    client: AsyncClient,
    account_id: str,
    token: str,
    *,
    name: str = "primary",
    release: str = "runtime-release-1",
) -> dict[str, Any]:
    response = await client.post(
        f"/v1/accounts/{account_id}/deployments",
        json={
            "name": name,
            "model_alias": "launch-model",
            "spec": {"runtime": {"release": release}, "replicas": 1},
        },
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def enroll_stamp(
    client: AsyncClient,
    account_id: str,
    token: str,
    *,
    name: str = "byoi-cluster",
    orchestrator: str = "k3s",
) -> dict[str, Any]:
    """Create an enrollment token and register a stamp with it."""
    token_response = await client.post(
        f"/v1/accounts/{account_id}/stamp-enrollment-tokens",
        json={"allowed_mode": "byoi", "expires_in_minutes": 30},
        headers=bearer(token),
    )
    assert token_response.status_code == 201, token_response.text
    enrollment_token = token_response.json()["enrollment_token"]

    enroll_response = await client.post(
        "/v1/stamps/enroll",
        json={
            "enrollment_token": enrollment_token,
            "name": name,
            "capabilities": {
                "orchestrator": orchestrator,
                "region": "local",
                "gpus": [{"product": "test-gpu", "count": 1, "memory_bytes": 1024}],
                "allocatable_gpus": 1,
                "requested_gpus": 0,
            },
        },
    )
    assert enroll_response.status_code == 201, enroll_response.text
    return enroll_response.json()
