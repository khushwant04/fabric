"""Following the agent's rewrites, and warming keys before traffic arrives.

Both behaviours were missing and both were invisible locally, where the
configuration file is written before the process starts and a test always sends a
request. Deploying to Kubernetes exposed them: a placement never took effect, and
the pod could never become ready.
"""

from __future__ import annotations

import json
import uuid

import pytest

from fabric_data_plane.errors import ApiError
from fabric_data_plane.registry import ReloadingRegistry

ACCOUNT = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER = uuid.UUID("22222222-2222-2222-2222-222222222222")


def write(path, *entries) -> None:
    path.write_text(json.dumps({"deployments": list(entries)}))


def entry(alias: str, account: uuid.UUID = ACCOUNT, deployment_id: str | None = None) -> dict:
    return {
        "deployment_id": deployment_id or str(uuid.uuid4()),
        "account_id": str(account),
        "model_alias": alias,
        "upstream_url": "http://model-host:8000",
        "upstream_model": "release-1",
    }


def test_a_new_placement_is_served_without_a_restart(tmp_path) -> None:
    path = tmp_path / "deployments.json"
    write(path)
    registry = ReloadingRegistry(path, min_interval=0.0)

    with pytest.raises(ApiError):
        registry.resolve("late-model", account_id=ACCOUNT)

    # The agent renders a placement after the process started.
    write(path, entry("late-model"))

    resolved = registry.resolve("late-model", account_id=ACCOUNT)
    assert resolved.model_alias == "late-model"
    assert registry.snapshot()["reloads"] == 1


def test_a_withdrawn_placement_stops_being_served(tmp_path) -> None:
    path = tmp_path / "deployments.json"
    write(path, entry("going-away"))
    registry = ReloadingRegistry(path, min_interval=0.0)
    assert registry.resolve("going-away", account_id=ACCOUNT)

    write(path)

    with pytest.raises(ApiError):
        registry.resolve("going-away", account_id=ACCOUNT)


def test_an_unchanged_file_is_not_reread(tmp_path) -> None:
    path = tmp_path / "deployments.json"
    write(path, entry("stable"))
    registry = ReloadingRegistry(path, min_interval=0.0)

    for _ in range(5):
        registry.resolve("stable", account_id=ACCOUNT)

    # Only the initial load; the common path costs a stat, not a parse.
    assert registry.snapshot()["reloads"] == 0


def test_an_unreadable_file_keeps_the_last_good_registry(tmp_path) -> None:
    path = tmp_path / "deployments.json"
    write(path, entry("keep-me"))
    registry = ReloadingRegistry(path, min_interval=0.0)
    assert registry.resolve("keep-me", account_id=ACCOUNT)

    # Truncated content: refusing traffic the stamp is placed to serve would be
    # worse than continuing with what was last valid.
    path.write_text("{not json")

    assert registry.resolve("keep-me", account_id=ACCOUNT)
    state = registry.snapshot()
    assert state["reload_failures"] == 1
    assert state["deployments"] == 1


def test_a_deleted_file_keeps_the_last_good_registry(tmp_path) -> None:
    path = tmp_path / "deployments.json"
    write(path, entry("keep-me"))
    registry = ReloadingRegistry(path, min_interval=0.0)
    assert registry.resolve("keep-me", account_id=ACCOUNT)

    path.unlink()

    assert registry.resolve("keep-me", account_id=ACCOUNT)


def test_reload_still_enforces_account_ownership(tmp_path) -> None:
    """A reload must not become a way to serve another account's deployment."""
    path = tmp_path / "deployments.json"
    write(path)
    registry = ReloadingRegistry(path, min_interval=0.0)

    write(path, entry("private", account=OTHER))

    with pytest.raises(ApiError):
        registry.resolve("private", account_id=ACCOUNT)
    assert registry.for_account(ACCOUNT) == []
    assert len(registry.for_account(OTHER)) == 1


async def test_keys_are_warmed_without_any_request(tmp_path, monkeypatch) -> None:
    """Readiness requires keys, so keys cannot wait for traffic that needs readiness."""
    import asyncio

    from fabric_data_plane import serve
    from tests.conftest import make_settings

    path = tmp_path / "deployments.json"
    write(path)
    settings = make_settings(deployments_file=str(path), jwks_refresh_seconds=1)

    class Cache:
        def __init__(self) -> None:
            self.refreshes = 0
            self.held = 0

        def snapshot(self) -> dict:
            return {"keys_held": self.held}

        def refresh(self) -> bool:
            self.refreshes += 1
            # Unreachable at first, as during a rolling restart of the control plane.
            if self.refreshes >= 2:
                self.held = 2
            return self.held > 0

    cache = Cache()
    plane = type("P", (), {"keys": cache})()

    warmer = asyncio.create_task(serve._warm_keys(plane, settings))
    for _ in range(100):
        if cache.held:
            break
        await asyncio.sleep(0.05)
    warmer.cancel()

    assert cache.held == 2, "keys were never warmed, so the pod would never be ready"
    assert cache.refreshes >= 2, "a failed first fetch was not retried"
