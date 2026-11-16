"""Serving metrics: pushed by the stamp, stored per minute, read as chart series.

The agent pushes compact samples (deltas, histogram buckets, gauges) every 30 seconds and the
control plane merges them into one row per series per minute (docs/serving-metrics.md).
These tests pin what matters: rates and percentiles come out right, samples in one minute
merge rather than duplicate, retention is enforced, a stamp cannot report for a deployment
not placed on it, and no account reads another's metrics. The row-level security half of
the isolation is in the PostgreSQL-only test at the bottom.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import account_context, reset_context, system_context
from app.models_serving_metrics import ServingMetricPoint
from app.services.serving_metrics import quantile
from tests.conftest import USING_POSTGRES
from tests.helpers import bearer, create_deployment, enroll_stamp, onboard, place


def minute(offset: int = 0) -> dt.datetime:
    """The start of the current minute plus ``offset`` minutes, a few seconds in."""
    now = dt.datetime.now(tz=dt.UTC).replace(second=0, microsecond=0)
    return now + dt.timedelta(minutes=offset, seconds=5)


def deployment_point(deployment_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "deployment_id": deployment_id,
        "requests": {"2xx": 57, "4xx": 2, "5xx": 1},
        "input_tokens": 12000,
        "output_tokens": 9000,
        "latency_seconds": {"0.5": 20, "1": 30, "2.5": 9, "+Inf": 1},
        "ttft_seconds": {"0.1": 50, "0.25": 10},
        "itl_seconds": {"0.025": 900},
        "hosts": 2,
        "running": 6,
        "waiting": 1,
        "kv_cache_percent": 40,
        "gpus": 2,
        "gpu_utilization_percent": 70,
        "gpu_memory_used_bytes": 40e9,
        "gpu_memory_total_bytes": 48e9,
    }
    body.update(overrides)
    return body


def gpu_point(index: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "node": "gpu-node-1",
        "index": index,
        "product": "NVIDIA A10",
        "utilization_percent": 80,
        "memory_used_bytes": 20e9,
        "memory_total_bytes": 24e9,
    }
    body.update(overrides)
    return body


def sample(at: dt.datetime, deployments=(), gpus=(), interval: float = 60) -> dict[str, Any]:
    return {
        "observed_at": at.isoformat(),
        "interval_seconds": interval,
        "sources": ["gateway", "model_host", "dcgm"],
        "deployments": list(deployments),
        "gpus": list(gpus),
    }


async def placed_stamp(
    client: AsyncClient, subject: str
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    account_id, token = await onboard(client, subject, f"{subject}-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token, name=f"{subject}-stamp")
    placed = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )
    assert placed.status_code == 201, placed.text
    return account_id, token, deployment, enrolled


async def send(client: AsyncClient, enrolled: dict[str, Any], *samples: dict[str, Any]):
    reset_context()
    return await client.post(
        f"/v1/stamps/{enrolled['stamp']['id']}/metrics",
        json={"samples": list(samples)},
        headers=bearer(enrolled["agent_credential"]),
    )


async def read(client: AsyncClient, account_id: str, token: str, path: str, window: str = "1h"):
    reset_context()
    return await client.get(
        f"/v1/accounts/{account_id}/{path}", params={"window": window}, headers=bearer(token)
    )


async def point_count(db_session: AsyncSession) -> int:
    await db_session.rollback()
    with system_context():
        count = (await db_session.execute(select(func.count(ServingMetricPoint.id)))).scalar_one()
    await db_session.rollback()
    return count


def latest(points: list[dict[str, Any]], field: str) -> dict[str, Any]:
    return [point for point in points if point[field] is not None][-1]


def test_quantile_interpolates_within_the_bucket() -> None:
    histogram = {"0.5": 20, "1": 30, "2.5": 9, "+Inf": 1}
    assert quantile(histogram, 0.5) == pytest.approx(0.5 + 0.5 * (10 / 30))
    assert quantile(histogram, 0.95) == pytest.approx(1 + 1.5 * (7 / 9))
    assert quantile(histogram, 1.0) == 2.5  # the +Inf bucket reports the largest bound
    assert quantile({}, 0.5) is None


async def test_reported_metrics_read_back_as_chart_series(client: AsyncClient) -> None:
    account_id, token, deployment, enrolled = await placed_stamp(client, "metrics-read")
    sent = await send(
        client,
        enrolled,
        sample(minute(-1), [deployment_point(deployment["id"])], [gpu_point("0"), gpu_point("1")]),
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["accepted_samples"] == 1
    assert sent.json()["ignored_deployments"] == []

    response = await read(client, account_id, token, f"deployments/{deployment['id']}/metrics")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["window"] == "1h" and body["step_seconds"] == 60
    assert len(body["points"]) == 60
    assert body["sources"] == ["dcgm", "gateway", "model_host"]
    assert body["reported_at"] is not None

    point = latest(body["points"], "requests_per_min")
    # 60 requests in a 60-second sample is 60 a minute; 9000 tokens is 150 a second.
    assert point["requests_per_min"] == pytest.approx(60)
    assert point["errors_per_min"] == pytest.approx(3)
    assert point["status_per_min"] == {"2xx": 57, "4xx": 2, "5xx": 1}
    assert point["output_tokens_per_s"] == pytest.approx(150)
    assert point["latency_p50_ms"] == pytest.approx(666.667, rel=1e-3)
    assert point["ttft_p50_ms"] == pytest.approx(60)
    assert point["running"] == 6 and point["waiting"] == 1
    assert point["kv_cache_percent"] == 40
    assert point["gpu_utilization_percent"] == 70
    assert point["gpu_memory_total_bytes"] == 48e9
    # Steps nobody reported for are empty, not zero, so a chart shows a gap.
    assert body["points"][0]["requests_per_min"] is None

    summary = body["summary"]
    assert summary["requests"] == 60 and summary["errors"] == 3
    assert summary["error_rate"] == pytest.approx(0.05)
    assert summary["ttft_p95_ms"] == pytest.approx(205)  # rank 57: 7 of 10 into (0.1, 0.25]

    stamp = await read(client, account_id, token, f"stamps/{enrolled['stamp']['id']}/metrics", "6h")
    assert stamp.status_code == 200, stamp.text
    assert stamp.headers["cache-control"] == "no-store"
    stamp_body = stamp.json()
    assert stamp_body["step_seconds"] == 300 and len(stamp_body["totals"]) == 72
    assert [(gpu["node"], gpu["index"]) for gpu in stamp_body["gpus"]] == [
        ("gpu-node-1", "0"),
        ("gpu-node-1", "1"),
    ]
    assert stamp_body["gpus"][0]["latest"]["utilization_percent"] == 80
    assert stamp_body["gpus"][0]["product"] == "NVIDIA A10"
    total = latest(stamp_body["totals"], "gpu_utilization_percent")
    assert total["gpu_memory_used_bytes"] == 40e9 and total["gpu_memory_total_bytes"] == 48e9
    assert total["requests_per_min"] == pytest.approx(60)

    overview = await read(client, account_id, token, "metrics", "24h")
    assert overview.status_code == 200, overview.text
    assert len(overview.json()["points"]) == 96
    assert overview.json()["summary"]["output_tokens"] == 9000


async def test_samples_in_one_minute_merge_and_rates_use_covered_time(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token, deployment, enrolled = await placed_stamp(client, "metrics-merge")
    at = minute(-1)
    first = deployment_point(
        deployment["id"], requests={"2xx": 10}, output_tokens=300, running=2, kv_cache_percent=20
    )
    second = deployment_point(
        deployment["id"], requests={"2xx": 20}, output_tokens=600, running=4, kv_cache_percent=40
    )
    await send(client, enrolled, sample(at, [first], interval=30))
    sent = await send(
        client, enrolled, sample(at + dt.timedelta(seconds=30), [second], interval=30)
    )
    assert sent.status_code == 200, sent.text
    assert await point_count(db_session) == 1

    body = (await read(client, account_id, token, f"deployments/{deployment['id']}/metrics")).json()
    point = latest(body["points"], "requests_per_min")
    assert point["requests_per_min"] == pytest.approx(30)  # 30 requests over 60 covered seconds
    assert point["output_tokens_per_s"] == pytest.approx(15)
    assert point["running"] == 3  # the mean of the samples
    assert point["kv_cache_percent"] == 30


async def test_an_idle_deployment_reports_zero_rather_than_nothing(client: AsyncClient) -> None:
    account_id, token, deployment, enrolled = await placed_stamp(client, "metrics-idle")
    idle = {"deployment_id": deployment["id"], "hosts": 1, "running": 0, "waiting": 0}
    await send(client, enrolled, sample(minute(-1), [idle]))
    body = (await read(client, account_id, token, f"deployments/{deployment['id']}/metrics")).json()
    point = latest(body["points"], "requests_per_min")
    assert point["requests_per_min"] == 0 and point["latency_p50_ms"] is None
    assert body["summary"]["requests"] == 0 and body["summary"]["error_rate"] is None


async def test_old_and_future_samples_are_skipped_and_retention_is_enforced(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token, deployment, enrolled = await placed_stamp(client, "metrics-retention")
    sent = await send(
        client,
        enrolled,
        sample(minute(-8 * 24 * 60), [deployment_point(deployment["id"])]),
        sample(minute(30), [deployment_point(deployment["id"])]),
        sample(minute(-3), [deployment_point(deployment["id"])]),
    )
    assert sent.json()["accepted_samples"] == 1 and sent.json()["skipped_samples"] == 2
    assert await point_count(db_session) == 1

    # A row past retention is deleted when the stamp next reports.
    await db_session.rollback()
    with system_context():
        await db_session.execute(
            update(ServingMetricPoint).values(
                bucket_start=dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=7, minutes=10)
            )
        )
        await db_session.commit()
    await send(client, enrolled, sample(minute(-1), [deployment_point(deployment["id"])]))
    assert await point_count(db_session) == 1

    week = (
        await read(client, account_id, token, f"deployments/{deployment['id']}/metrics", "7d")
    ).json()
    assert week["step_seconds"] == 3600 and len(week["points"]) == 168
    bad = await read(client, account_id, token, f"deployments/{deployment['id']}/metrics", "30d")
    assert bad.status_code == 422


async def test_another_account_cannot_read_the_metrics(client: AsyncClient) -> None:
    account_a, token_a, deployment, enrolled = await placed_stamp(client, "metrics-iso-a")
    account_b, token_b, deployment_b, enrolled_b = await placed_stamp(client, "metrics-iso-b")
    await send(
        client, enrolled, sample(minute(-1), [deployment_point(deployment["id"])], [gpu_point("0")])
    )

    path = f"deployments/{deployment['id']}/metrics"
    crossed = await read(client, account_a, token_b, path)
    assert crossed.status_code == 403
    assert crossed.json()["error"]["code"] == "account_mismatch"
    assert (await read(client, account_b, token_b, path)).status_code == 404

    stamp_path = f"stamps/{enrolled['stamp']['id']}/metrics"
    assert (await read(client, account_a, token_b, stamp_path)).status_code == 403
    assert (await read(client, account_b, token_b, stamp_path)).status_code == 404

    # B's overview and B's own stamp see none of A's traffic or GPUs.
    overview = (await read(client, account_b, token_b, "metrics")).json()
    assert overview["summary"]["requests"] == 0 and overview["reported_at"] is None
    own = (
        await read(client, account_b, token_b, f"stamps/{enrolled_b['stamp']['id']}/metrics")
    ).json()
    assert own["gpus"] == []

    mine = (await read(client, account_a, token_a, path)).json()
    assert mine["summary"]["requests"] == 60


async def test_a_stamp_cannot_report_for_a_deployment_not_placed_on_it(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_a, token_a, deployment_a, _enrolled_a = await placed_stamp(client, "metrics-own-a")
    _account_b, _token_b, deployment_b, enrolled_b = await placed_stamp(client, "metrics-own-b")
    unknown = str(uuid.uuid4())
    sent = await send(
        client,
        enrolled_b,
        sample(
            minute(-1),
            [
                deployment_point(deployment_b["id"]),
                deployment_point(deployment_a["id"]),
                deployment_point(unknown),
            ],
            # Labelling a device with another account's deployment does not stick either.
            [
                gpu_point("0", deployment_id=deployment_a["id"]),
                gpu_point("1", deployment_id=deployment_b["id"]),
            ],
        ),
    )
    assert sent.status_code == 200, sent.text
    assert sorted(sent.json()["ignored_deployments"]) == sorted([deployment_a["id"], unknown])
    assert await point_count(db_session) == 3  # B's deployment and B's two GPUs

    smuggled = (
        await read(client, account_a, token_a, f"deployments/{deployment_a['id']}/metrics")
    ).json()
    assert smuggled["reported_at"] is None and smuggled["summary"]["requests"] == 0

    await db_session.rollback()
    with system_context():
        labels = {
            row.series: (row.data or {}).get("deployment_id")
            for row in (
                await db_session.execute(
                    select(ServingMetricPoint).where(ServingMetricPoint.kind == "gpu")
                )
            ).scalars()
        }
    await db_session.rollback()
    assert labels == {"gpu-node-1/0": None, "gpu-node-1/1": deployment_b["id"]}


async def test_metric_reports_require_the_stamps_own_agent_credential(client: AsyncClient) -> None:
    _account, _token, deployment, enrolled = await placed_stamp(client, "metrics-auth-a")
    _account_b, _token_b, _deployment_b, other = await placed_stamp(client, "metrics-auth-b")
    body = {"samples": [sample(minute(-1), [deployment_point(deployment["id"])])]}
    path = f"/v1/stamps/{enrolled['stamp']['id']}/metrics"

    reset_context()
    telemetry = await client.post(path, json=body, headers=bearer(enrolled["telemetry_credential"]))
    assert telemetry.status_code == 401

    reset_context()
    mismatched = await client.post(path, json=body, headers=bearer(other["agent_credential"]))
    assert mismatched.status_code == 403
    assert mismatched.json()["error"]["code"] == "stamp_mismatch"

    reset_context()
    assert (await client.post(path, json=body)).status_code == 401


async def test_metric_reports_are_bounded(client: AsyncClient) -> None:
    _account, _token, deployment, enrolled = await placed_stamp(client, "metrics-bounds")
    point = deployment_point(deployment["id"])

    too_many_samples = await send(client, enrolled, *[sample(minute(-1), [point])] * 21)
    assert too_many_samples.status_code == 422
    too_many_buckets = await send(
        client,
        enrolled,
        sample(minute(-1), [{**point, "latency_seconds": {str(i): 1 for i in range(65)}}]),
    )
    assert too_many_buckets.status_code == 422
    unknown_class = await send(
        client, enrolled, sample(minute(-1), [{**point, "requests": {"3xx": 1}}])
    )
    assert unknown_class.status_code == 422
    smuggled = await send(client, enrolled, sample(minute(-1), [{**point, "account_id": "x"}]))
    assert smuggled.status_code == 422
    negative = await send(client, enrolled, sample(minute(-1), [{**point, "output_tokens": -1}]))
    assert negative.status_code == 422
    bad_bound = await send(
        client, enrolled, sample(minute(-1), [{**point, "ttft_seconds": {"fast": 1}}])
    )
    assert bad_bound.status_code == 422
    empty = await send(client, enrolled)
    assert empty.status_code == 422


async def test_deleting_a_deployment_hides_its_metrics(client: AsyncClient) -> None:
    account_id, token, deployment, enrolled = await placed_stamp(client, "metrics-delete")
    await send(client, enrolled, sample(minute(-1), [deployment_point(deployment["id"])]))
    reset_context()
    deleted = await client.delete(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}", headers=bearer(token)
    )
    assert deleted.status_code == 204, deleted.text
    gone = await read(client, account_id, token, f"deployments/{deployment['id']}/metrics")
    assert gone.status_code == 404
    late = await send(client, enrolled, sample(minute(0), [deployment_point(deployment["id"])]))
    assert late.json()["ignored_deployments"] == [deployment["id"]]


# --- row-level security (PostgreSQL only) ---------------------------------------------

postgres_only = pytest.mark.skipif(
    not USING_POSTGRES, reason="row-level security needs PostgreSQL; set FABRIC_TEST_DATABASE_URL"
)


@postgres_only
async def test_metric_points_are_isolated_by_row_level_security(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An unfiltered query returns only the declared account's points, and an account cannot
    write a point into another account's deployment."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError, ProgrammingError

    account_a, _token_a, deployment_a, enrolled_a = await placed_stamp(client, "metrics-rls-a")
    account_b, _token_b, deployment_b, enrolled_b = await placed_stamp(client, "metrics-rls-b")
    await send(
        client,
        enrolled_a,
        sample(minute(-1), [deployment_point(deployment_a["id"])], [gpu_point("0")]),
    )
    await send(client, enrolled_b, sample(minute(-1), [deployment_point(deployment_b["id"])]))

    with system_context():
        flags = (
            await db_session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'serving_metric_points'"
                )
            )
        ).one()
    assert flags == (True, True)
    await db_session.rollback()

    reset_context()
    with account_context(uuid.UUID(account_a)):
        visible = sorted((await db_session.execute(select(ServingMetricPoint.series))).scalars())
        assert visible == sorted([deployment_a["id"], "gpu-node-1/0"])
    await db_session.rollback()

    with account_context(uuid.UUID(account_b)):
        visible = list((await db_session.execute(select(ServingMetricPoint.series))).scalars())
        assert visible == [deployment_b["id"]]
    await db_session.rollback()

    reset_context()
    count = (await db_session.execute(select(func.count(ServingMetricPoint.id)))).scalar_one()
    assert count == 0
    await db_session.rollback()

    with account_context(uuid.UUID(account_a)):
        db_session.add(
            ServingMetricPoint(
                account_id=uuid.UUID(account_b),
                stamp_id=uuid.UUID(enrolled_b["stamp"]["id"]),
                deployment_id=uuid.UUID(deployment_b["id"]),
                kind="deployment",
                series="forged",
                bucket_start=minute(-2),
                samples=1,
                seconds=60,
                data={"requests": {"2xx": 1000}},
                updated_at=dt.datetime.now(tz=dt.UTC),
            )
        )
        with pytest.raises((DBAPIError, ProgrammingError)) as failure:
            await db_session.flush()
    assert "row-level security" in str(failure.value).lower()
    await db_session.rollback()
