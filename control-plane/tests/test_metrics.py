"""Metrics ingestion.

Usage answers what an account owes; metrics answer whether a stamp is healthy. The
authorization rules are the same as usage, and for the same reason: a stamp must not be
able to report as another one.
"""

from __future__ import annotations

import datetime as dt

from httpx import AsyncClient

from app.services.metrics import summarize
from tests.helpers import bearer, enroll_stamp, onboard

METRICS = "/v1/telemetry/metrics"


def device(index: int = 0, **overrides) -> dict:
    sample = {
        "index": index,
        "product": "NVIDIA GeForce RTX 4070 Laptop GPU",
        "utilization_gpu_percent": 42.0,
        "utilization_memory_percent": 30.0,
        "memory_used_mib": 5045.0,
        "memory_total_mib": 8188.0,
        "temperature_c": 61.0,
        "power_draw_w": 45.5,
        "sm_clock_mhz": 1980.0,
        "sm_clock_max_mhz": 3105.0,
    }
    sample.update(overrides)
    return sample


def report(**overrides) -> dict:
    body = {
        "collected_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "gpus": [device()],
        "runtime": {
            "source": "http://127.0.0.1:8000/metrics",
            "reachable": True,
            "values": {"vllm:num_requests_running": 2.0},
        },
    }
    body.update(overrides)
    return body


async def test_a_stamp_reports_its_own_metrics(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "metrics-user", "metrics-account")
    enrolled = await enroll_stamp(client, account_id, token)

    response = await client.post(
        METRICS, json=report(), headers=bearer(enrolled["telemetry_credential"])
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stamp_id"] == enrolled["stamp"]["id"]
    assert body["gpus_recorded"] == 1
    assert body["runtime_recorded"] is True


async def test_the_agent_credential_cannot_report_metrics(client: AsyncClient) -> None:
    """Same separation as usage: only the write-only telemetry credential is accepted."""
    account_id, token = await onboard(client, "metrics-sep", "metrics-sep-account")
    enrolled = await enroll_stamp(client, account_id, token)

    response = await client.post(
        METRICS, json=report(), headers=bearer(enrolled["agent_credential"])
    )
    assert response.status_code == 401


async def test_a_revoked_stamp_cannot_report_metrics(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "metrics-rev", "metrics-rev-account")
    enrolled = await enroll_stamp(client, account_id, token)

    await client.delete(
        f"/v1/accounts/{account_id}/stamps/{enrolled['stamp']['id']}", headers=bearer(token)
    )

    response = await client.post(
        METRICS, json=report(), headers=bearer(enrolled["telemetry_credential"])
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "credential_revoked"


async def test_a_report_cannot_name_another_stamp(client: AsyncClient) -> None:
    """The contract carries no stamp or account: both come from the credential."""
    account_id, token = await onboard(client, "metrics-smuggle", "metrics-smuggle-account")
    enrolled = await enroll_stamp(client, account_id, token)

    payload = report()
    payload["stamp_id"] = "11111111-1111-1111-1111-111111111111"

    response = await client.post(
        METRICS, json=payload, headers=bearer(enrolled["telemetry_credential"])
    )
    assert response.status_code == 422


async def test_an_unreachable_runtime_is_accepted_with_its_cause(client: AsyncClient) -> None:
    """A host still loading weights reports no runtime metrics, which is not an error."""
    account_id, token = await onboard(client, "metrics-loading", "metrics-loading-account")
    enrolled = await enroll_stamp(client, account_id, token)

    response = await client.post(
        METRICS,
        json=report(
            runtime={
                "source": "http://127.0.0.1:8000/metrics",
                "reachable": False,
                "unreachable_cause": "connection refused",
            }
        ),
        headers=bearer(enrolled["telemetry_credential"]),
    )

    assert response.status_code == 200
    assert response.json()["runtime_recorded"] is False


async def test_the_latest_sample_is_readable_on_the_stamp(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "metrics-read", "metrics-read-account")
    enrolled = await enroll_stamp(client, account_id, token)

    await client.post(
        METRICS, json=report(), headers=bearer(enrolled["telemetry_credential"])
    )

    stamps = await client.get(f"/v1/accounts/{account_id}/stamps", headers=bearer(token))
    assert stamps.status_code == 200
    metrics = stamps.json()[0]["capabilities"]["metrics"]
    assert metrics["gpu"]["devices"] == 1
    assert metrics["runtime"]["reachable"] is True


def test_the_busiest_device_is_what_a_summary_reports() -> None:
    """Averaging a saturated device with idle ones hides the condition worth seeing."""
    summary = summarize([
        device(0, utilization_gpu_percent=99.0, temperature_c=84.0),
        device(1, utilization_gpu_percent=1.0, temperature_c=40.0),
    ])

    assert summary["devices"] == 2
    assert summary["max_utilization_gpu_percent"] == 99.0
    assert summary["max_temperature_c"] == 84.0
    # Memory is a total, because capacity is what a placement decision needs.
    assert summary["memory_total_mib"] == 8188.0 * 2


def test_the_slowest_clock_is_what_limits_a_stamp() -> None:
    summary = summarize([
        device(0, sm_clock_mhz=3105.0, sm_clock_max_mhz=3105.0),
        device(1, sm_clock_mhz=1552.0, sm_clock_max_mhz=3105.0),
    ])
    assert 0.49 < summary["min_sm_clock_fraction"] < 0.51


def test_no_devices_summarises_to_nothing() -> None:
    """A stamp with no GPU is a configuration, not an error, so there is nothing to say."""
    assert summarize([]) == {}
