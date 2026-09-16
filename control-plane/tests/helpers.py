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
    replicas: int = 1,
    resources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {"runtime": {"release": release}, "replicas": replicas}
    if resources is not None:
        spec["resources"] = resources
    response = await client.post(
        f"/v1/accounts/{account_id}/deployments",
        json={"name": name, "model_alias": "launch-model", "spec": spec},
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def place(
    client: AsyncClient,
    account_id: str,
    token: str,
    deployment_id: str,
    *,
    stamp_id: str | None = None,
    region: str | None = None,
) -> Response:
    """Ask for a placement, naming a stamp or letting the platform choose one."""
    body: dict[str, Any] = {}
    if stamp_id is not None:
        body["stamp_id"] = stamp_id
    if region is not None:
        body["region"] = region
    return await client.post(
        f"/v1/accounts/{account_id}/deployments/{deployment_id}/placements",
        json=body,
        headers=bearer(token),
    )


# A real Tesla T4 advertises 15360 MiB, not a round 16 GiB. Fixtures use the real number so
# the class catalogue's memory tolerance is actually exercised rather than assumed away.
T4_MEMORY_BYTES = 15360 * 1024 * 1024


def default_capabilities(
    *,
    orchestrator: str = "k3s",
    region: str | None = "local",
    gpus: int = 1,
    requested_gpus: int = 0,
    fabric_requested_gpus: int = 0,
    product: str = "Tesla T4",
    memory_bytes: int = T4_MEMORY_BYTES,
    compute_capability: str | None = "7.5",
    max_gpus_per_node: int | None = None,
) -> dict[str, Any]:
    """A capability report describing hardware that could really serve something.

    Placement is admitted against these numbers (ADR 0013), so a fixture reporting
    ``memory_bytes: 1024`` would describe a stamp with one kibibyte of video memory and make
    every test assert against a refusal.
    """
    return {
        "orchestrator": orchestrator,
        "region": region,
        "gpus": [
            {
                "product": product,
                "count": gpus,
                "memory_bytes": memory_bytes,
                **({"compute_capability": compute_capability} if compute_capability else {}),
            }
        ],
        "allocatable_gpus": gpus,
        "requested_gpus": requested_gpus,
        "fabric_requested_gpus": fabric_requested_gpus,
        # A measuring agent says so; zero claimed devices is otherwise indistinguishable from
        # an idle cluster.
        "gpu_claims_measured": True,
        "max_gpus_per_node": gpus if max_gpus_per_node is None else max_gpus_per_node,
    }


async def enroll_stamp(
    client: AsyncClient,
    account_id: str,
    token: str,
    *,
    name: str = "byoi-cluster",
    orchestrator: str = "k3s",
    capabilities: dict[str, Any] | None = None,
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
            # Eight devices by default, so a test about generations, credentials or status
            # is not incidentally a test about capacity. A test that is about capacity passes
            # its own smaller report.
            "capabilities": capabilities
            or default_capabilities(orchestrator=orchestrator, gpus=8),
        },
    )
    assert enroll_response.status_code == 201, enroll_response.text
    return enroll_response.json()



async def enroll_managed_stamp(
    client: AsyncClient,
    db_session: Any,
    *,
    orchestrator: str = "aks",
    name: str | None = None,
    capabilities: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Any]:
    """Register a managed stamp owned by the protected Fabric system account."""
    # Imported here so the module stays importable without the application's service
    # layer, which the pure-HTTP helpers above do not need.
    from app.services.accounts import ensure_system_account
    from app.services.stamps import create_enrollment_token

    system_account = await ensure_system_account(db_session)
    _record, enrollment_token = await create_enrollment_token(
        db_session,
        account=system_account,
        allowed_mode="managed",
        expires_in_minutes=30,
        actor_user_id=None,
    )
    await db_session.commit()

    report = capabilities or default_capabilities(orchestrator=orchestrator, gpus=8)
    report = {**report, "orchestrator": orchestrator}
    enrolled = await client.post(
        "/v1/stamps/enroll",
        json={
            "enrollment_token": enrollment_token,
            "name": name or f"managed-{orchestrator}",
            "capabilities": report,
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    return enrolled.json(), system_account


async def enable_managed_capacity(db_session: Any, account_id: str) -> None:
    """Grant an account the entitlement managed placement requires.

    Under a system context because this writes another account's row from no request. With
    policies active an undeclared update matches nothing and silently grants nothing, which
    then looks like an entitlement bug in whatever is being tested.
    """
    import uuid as _uuid

    from sqlalchemy import update as _update

    from app.core.tenancy import system_context as _system_context
    from app.models import Account as _Account

    with _system_context():
        await db_session.execute(
            _update(_Account)
            .where(_Account.id == _uuid.UUID(account_id))
            .values(managed_capacity_enabled=True)
        )
        await db_session.commit()



async def report_capabilities(
    client: AsyncClient,
    stamp_id: str,
    agent_credential: str,
    capabilities: dict[str, Any],
) -> Response:
    """Heartbeat a refreshed capability report, as a running agent does every poll."""
    return await client.post(
        f"/v1/stamps/{stamp_id}/heartbeat",
        json={"capabilities": capabilities},
        headers=bearer(agent_credential),
    )


def with_claims(
    capabilities: dict[str, Any],
    claims: dict[str, int],
    *,
    foreign_gpus: int = 0,
    untracked_gpus: int = 0,
) -> dict[str, Any]:
    """A capability report where named deployments are holding devices.

    This is what a stamp looks like once the operator has actually started hosts: claims are
    reported per deployment so the control plane can compare each against what it committed
    for that deployment (ADR 0013).
    """
    fabric = sum(claims.values()) + untracked_gpus
    return {
        **capabilities,
        "fabric_gpu_claims": [
            {"deployment_id": deployment, "gpus": gpus} for deployment, gpus in claims.items()
        ],
        "fabric_requested_gpus": fabric,
        "requested_gpus": fabric + foreign_gpus,
        "gpu_claims_measured": True,
    }
