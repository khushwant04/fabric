"""Ingress-level proof that least-in-flight tracks real active attempts."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from fabric_data_plane.app import DataPlane, create_inference_app
from fabric_data_plane.keys import KeyCache
from fabric_data_plane.pool import Backend
from fabric_data_plane.registry import Deployment, DeploymentRegistry
from tests.conftest import ACCOUNT_A, DEPLOYMENT_A, SigningKey, make_settings


class GatedFleet:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.gate = asyncio.Event()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.host)
        await self.gate.wait()
        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "model": request.url.host,
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _plane(fleet: GatedFleet, control_plane) -> DataPlane:
    settings = make_settings(max_in_flight_per_account=64)
    return DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=control_plane.client()),
        registry=DeploymentRegistry(
            [
                Deployment(
                    deployment_id=DEPLOYMENT_A,
                    account_id=ACCOUNT_A,
                    model_alias="launch-model",
                    backends=tuple(
                        Backend(url=f"http://{name}.test", backend_id=f"pod-{name}")
                        for name in ("a", "b", "c")
                    ),
                    strategy="least_in_flight",
                )
            ]
        ),
        client=fleet.client(),
    )


async def test_least_in_flight_spreads_real_concurrent_requests_and_releases_counts(
    control_plane, signing_key: SigningKey
) -> None:
    fleet = GatedFleet()
    plane = _plane(fleet, control_plane)
    app = create_inference_app(plane)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {signing_key.issue()}"}
    payload = {"model": "launch-model", "messages": [{"role": "user", "content": "hi"}]}

    async with httpx.AsyncClient(transport=transport, base_url="http://ingress.test") as http:
        tasks = [
            asyncio.create_task(
                http.post("/v1/chat/completions", json=payload, headers=headers)
            )
            for _ in range(3)
        ]
        for _ in range(200):
            if len(fleet.requests) == 3:
                break
            await asyncio.sleep(0.01)

        assert set(fleet.requests) == {"a.test", "b.test", "c.test"}
        deployment = plane.registry.resolve("launch-model", account_id=ACCOUNT_A)
        pool = plane.pool_for(deployment)
        assert sum(pool.health.in_flight().values()) == 3
        rendered = plane.metrics.render()
        assert rendered.count("fabric_dp_backend_in_flight{") == 3
        assert all(f'backend_id="pod-{name}"}} 1' in rendered for name in ("a", "b", "c"))

        fleet.gate.set()
        responses = await asyncio.gather(*tasks)

    assert all(response.status_code == 200 for response in responses)
    assert sum(pool.health.in_flight().values()) == 0
    drained = plane.metrics.render()
    assert all(f'backend_id="pod-{name}"}} 0' in drained for name in ("a", "b", "c"))



async def test_cancelled_request_releases_backend_and_request_leases(
    control_plane, signing_key: SigningKey
) -> None:
    fleet = GatedFleet()
    plane = _plane(fleet, control_plane)
    app = create_inference_app(plane)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {signing_key.issue()}"}
    payload = {"model": "launch-model", "messages": [{"role": "user", "content": "hi"}]}

    async with httpx.AsyncClient(transport=transport, base_url="http://ingress.test") as http:
        task = asyncio.create_task(
            http.post("/v1/chat/completions", json=payload, headers=headers)
        )
        for _ in range(200):
            if fleet.requests:
                break
            await asyncio.sleep(0.01)
        assert fleet.requests

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    deployment = plane.registry.resolve("launch-model", account_id=ACCOUNT_A)
    pool = plane.pool_for(deployment)
    assert sum(pool.health.in_flight().values()) == 0
    rendered = plane.metrics.render()
    assert 'backend_id="pod-a",outcome="cancelled"} 1' in rendered
    assert 'backend_id="pod-a"} 0' in rendered
