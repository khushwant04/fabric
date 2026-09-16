"""Private router-state acknowledgement used by zero-downtime rollout drain."""

from __future__ import annotations

import httpx

from fabric_data_plane.app import DataPlane, create_router_status_app
from fabric_data_plane.keys import KeyCache
from fabric_data_plane.pool import Backend
from fabric_data_plane.registry import Deployment, DeploymentRegistry
from tests.conftest import ACCOUNT_A, DEPLOYMENT_A, make_settings


def _plane(control_plane) -> tuple[DataPlane, Deployment]:
    settings = make_settings()
    deployment = Deployment(
        deployment_id=DEPLOYMENT_A,
        account_id=ACCOUNT_A,
        model_alias="launch-model",
        backends=(
            Backend(
                url="http://old.test", backend_id="old-pod", weight=0, workload="active"
            ),
            Backend(
                url="http://new.test", backend_id="new-pod", weight=1, workload="candidate"
            ),
        ),
        strategy="weighted",
        route_revision="deployment-cutover",
    )
    return (
        DataPlane(
            settings=settings,
            keys=KeyCache(settings, client=control_plane.client()),
            registry=DeploymentRegistry([deployment], revision="cutover-revision"),
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
        ),
        deployment,
    )


async def test_router_state_reports_loaded_revision_and_active_backend_attempts(
    control_plane,
) -> None:
    plane, deployment = _plane(control_plane)
    pool = plane.pool_for(deployment)
    old = pool.backends[0]
    pool.health.acquire(old)

    app = create_router_status_app(plane)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://router.test") as client:
        before = (await client.get("/router-state")).json()
        pool.health.release(old)
        after = (await client.get("/router-state")).json()

    assert before["revision"] == "cutover-revision"
    assert {entry["route_revision"] for entry in before["backends"]} == {
        "deployment-cutover"
    }
    by_id = {entry["backend_id"]: entry["in_flight"] for entry in before["backends"]}
    assert by_id == {"old-pod": 1, "new-pod": 0}
    drained = {entry["backend_id"]: entry["in_flight"] for entry in after["backends"]}
    assert drained["old-pod"] == 0


def test_adding_candidate_ids_preserves_old_active_attempts(control_plane) -> None:
    settings = make_settings()
    old_only = Deployment(
        deployment_id=DEPLOYMENT_A,
        account_id=ACCOUNT_A,
        model_alias="launch-model",
        backends=(Backend(url="http://old.test", backend_id="old-pod", workload="active"),),
    )
    registry = DeploymentRegistry([old_only], revision="old")
    plane = DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=control_plane.client()),
        registry=registry,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )
    old_pool = plane.pool_for(old_only)
    old_pool.health.acquire(old_pool.backends[0])

    cutover = Deployment(
        deployment_id=DEPLOYMENT_A,
        account_id=ACCOUNT_A,
        model_alias="launch-model",
        backends=(
            Backend(
                url="http://old.test", backend_id="old-pod", weight=0, workload="active"
            ),
            Backend(
                url="http://new.test", backend_id="new-pod", weight=1, workload="candidate"
            ),
        ),
        strategy="weighted",
        route_revision="deployment-cutover",
    )
    plane.registry = DeploymentRegistry([cutover], revision="cutover")
    new_pool = plane.pool_for(cutover)

    assert new_pool.health is old_pool.health
    assert new_pool.health.in_flight()["old-pod"] == 1
    assert new_pool.select().backend_id == "new-pod"



def test_removed_backend_identity_remains_visible_until_its_stream_finishes(control_plane) -> None:
    settings = make_settings()
    old = Deployment(
        deployment_id=DEPLOYMENT_A,
        account_id=ACCOUNT_A,
        model_alias="launch-model",
        backends=(Backend(url="http://old.test", backend_id="old-pod", workload="active"),),
        route_revision="before",
    )
    plane = DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=control_plane.client()),
        registry=DeploymentRegistry([old], revision="g1"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )
    old_pool = plane.pool_for(old)
    old_pool.health.acquire(old_pool.backends[0])

    candidate_only = Deployment(
        deployment_id=DEPLOYMENT_A,
        account_id=ACCOUNT_A,
        model_alias="launch-model",
        backends=(Backend(url="http://new.test", backend_id="new-pod", workload="candidate"),),
        route_revision="after",
    )
    plane.registry = DeploymentRegistry([candidate_only], revision="g2")
    new_pool = plane.pool_for(candidate_only)
    state = plane.router_state()
    by_id = {entry["backend_id"]: entry for entry in state["backends"]}

    assert new_pool.health is old_pool.health
    assert by_id["old-pod"]["in_flight"] == 1
    assert by_id["old-pod"]["retired"] is True
    assert by_id["old-pod"]["workload"] == "active"
    assert by_id["old-pod"]["route_revision"] == "after"
