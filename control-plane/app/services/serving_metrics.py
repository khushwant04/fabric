"""Serving metrics: per-minute traffic and GPU series pushed by stamps, read as charts.

A stamp's agent samples its gateway, its model hosts' vLLM metrics and any DCGM exporter
every 30 seconds and pushes compact aggregates (docs/serving-metrics.md). The control plane
never connects to a stamp, and the central Prometheus cannot see stamps outside its own
cluster, so this push is the only way the console can chart a model serving.

Storage is one row per series per minute per stamp. Samples landing in the same minute
merge into its row: request counts, token counts and latency-histogram buckets add; gauges
(queue depth, KV cache, GPU utilisation and memory) keep a sum and a sample count so their
mean can be read back. ``seconds`` records how much time the merged samples cover, and
every rate is a count divided by it, per stamp, so a stamp that missed a report does not
read as a traffic dip and two stamps serving one deployment add up rather than average.

Rows are kept for seven days at one-minute resolution and then deleted as new samples
arrive. Reads aggregate the minutes into chart steps (1 minute for an hour, 5 for six
hours, 15 for a day, 60 for a week); percentiles are computed from the merged histograms,
never averaged.

Deployment rows belong to the deployment's account (from the placement, never from the
report) and GPU rows to the stamp's account. Both are covered by the forced row-level
security policy every account table has.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, NotFound
from app.core.timeutil import as_utc, utc_now
from app.models import (
    MODE_BYOI,
    MODE_MANAGED,
    Deployment,
    DeploymentPlacement,
    InferenceStamp,
)
from app.models_serving_metrics import KIND_DEPLOYMENT, KIND_GPU, ServingMetricPoint
from app.schemas_serving_metrics import (
    AccountMetricsPoint,
    AccountMetricsResponse,
    DeploymentMetricsPoint,
    DeploymentMetricsResponse,
    GPUSeries,
    GPUSeriesPoint,
    MetricReportRequest,
    MetricsSummary,
    StampMetricsPoint,
    StampMetricsResponse,
)
from app.services.stamps import get_stamp

#: How long points are kept. Older minutes are deleted as new samples arrive.
RETENTION = dt.timedelta(days=7)
#: A stamp clock slightly ahead of ours is tolerated; further ahead is a broken clock.
FUTURE_TOLERANCE = dt.timedelta(minutes=5)
BUCKET_SECONDS = 60

#: window -> (length, chart step), in seconds. Each window charts 60 to 168 steps.
WINDOWS: dict[str, tuple[int, int]] = {
    "1h": (3600, 60),
    "6h": (6 * 3600, 300),
    "24h": (24 * 3600, 900),
    "7d": (7 * 24 * 3600, 3600),
}

ERROR_CLASSES = ("4xx", "5xx")
#: Gauges that add across stamps or deployments (a queue on two stamps is both queues);
#: the others are percentages and are averaged.
SUMMED_GAUGES = frozenset(
    {"running", "waiting", "gpu_mem_used", "gpu_mem_total", "mem_used", "mem_total"}
)


# --- ingest -----------------------------------------------------------------------------


@dataclasses.dataclass
class IngestResult:
    accepted_at: dt.datetime
    accepted_samples: int
    skipped_samples: int
    ignored_deployments: list[uuid.UUID]


def _floor(moment: dt.datetime, seconds: int) -> dt.datetime:
    epoch = int(moment.timestamp())
    return dt.datetime.fromtimestamp(epoch - epoch % seconds, tz=dt.UTC)


def _add_gauge(gauges: dict[str, list[float]], name: str, value: float | None) -> None:
    if value is None:
        return
    entry = gauges.setdefault(name, [0.0, 0.0])
    entry[0] += float(value)
    entry[1] += 1


def _merge_counts(target: dict[str, float], source: dict[str, float] | None) -> None:
    for key, value in (source or {}).items():
        target[key] = target.get(key, 0.0) + float(value)


async def _owners(session: AsyncSession, stamp: InferenceStamp) -> dict[uuid.UUID, uuid.UUID]:
    """Deployments placed on the stamp, with their accounts, by the status-report rule."""
    placements = (
        await session.execute(
            select(DeploymentPlacement.deployment_id, DeploymentPlacement.account_id)
            .join(
                Deployment,
                (Deployment.id == DeploymentPlacement.deployment_id)
                & (Deployment.account_id == DeploymentPlacement.account_id),
            )
            .where(DeploymentPlacement.stamp_id == stamp.id, Deployment.deleted_at.is_(None))
        )
    ).all()
    owner: dict[uuid.UUID, uuid.UUID] = {}
    for deployment_id, account_id in placements:
        if stamp.mode == MODE_BYOI and account_id != stamp.account_id:
            continue
        owner[deployment_id] = account_id
    return owner


async def ingest_metrics(
    session: AsyncSession, *, stamp_id: uuid.UUID, report: MetricReportRequest
) -> IngestResult:
    """Merge a report's samples into per-minute rows and drop rows past retention.

    ``stamp_id`` comes from the authenticated credential. A deployment not placed on the
    stamp is ignored, as in host reports, rather than failing the samples around it.
    """
    stamp = await get_stamp(session, stamp_id)
    if stamp.mode not in (MODE_BYOI, MODE_MANAGED):
        raise Forbidden("unsupported_stamp_mode", "Stamp ownership mode is not supported")
    owner = await _owners(session, stamp)

    now = utc_now()
    oldest = now - RETENTION
    # (kind, series, bucket) -> pending contribution
    pending: dict[tuple[str, str, dt.datetime], dict[str, Any]] = {}
    ignored: set[uuid.UUID] = set()
    accepted = skipped = 0

    for sample in report.samples:
        observed = as_utc(sample.observed_at) or now
        if observed < oldest or observed > now + FUTURE_TOLERANCE:
            skipped += 1
            continue
        observed = min(observed, now)
        bucket = _floor(observed, BUCKET_SECONDS)
        accepted += 1
        sources = sorted(set(sample.sources))
        seen: set[uuid.UUID] = set()
        for point in sample.deployments:
            account_id = owner.get(point.deployment_id)
            if account_id is None:
                ignored.add(point.deployment_id)
                continue
            if point.deployment_id in seen:
                continue
            seen.add(point.deployment_id)
            key = (KIND_DEPLOYMENT, str(point.deployment_id), bucket)
            entry = pending.setdefault(
                key,
                {
                    "account_id": account_id,
                    "deployment_id": point.deployment_id,
                    "samples": 0,
                    "seconds": 0.0,
                    "data": _empty_deployment_data(),
                },
            )
            entry["samples"] += 1
            entry["seconds"] += sample.interval_seconds
            data = entry["data"]
            _merge_counts(data["requests"], point.requests)
            data["input_tokens"] += point.input_tokens
            data["output_tokens"] += point.output_tokens
            _merge_counts(data["latency"], point.latency_seconds)
            _merge_counts(data["ttft"], point.ttft_seconds)
            _merge_counts(data["itl"], point.itl_seconds)
            gauges = data["gauges"]
            _add_gauge(gauges, "running", point.running)
            _add_gauge(gauges, "waiting", point.waiting)
            _add_gauge(gauges, "kv", point.kv_cache_percent)
            if point.gpus:
                _add_gauge(gauges, "gpu_util", point.gpu_utilization_percent)
                _add_gauge(gauges, "gpu_mem_used", point.gpu_memory_used_bytes)
                _add_gauge(gauges, "gpu_mem_total", point.gpu_memory_total_bytes)
            data["hosts"] = max(data["hosts"], point.hosts)
            data["gpus"] = max(data["gpus"], point.gpus)
            data["sources"] = sorted(set(data["sources"]) | set(sources))

        devices: set[str] = set()
        for gpu in sample.gpus:
            series = f"{gpu.node}/{gpu.index}"
            if series in devices:
                continue
            devices.add(series)
            key = (KIND_GPU, series, bucket)
            entry = pending.setdefault(
                key,
                {
                    "account_id": stamp.account_id,
                    "deployment_id": None,
                    "samples": 0,
                    "seconds": 0.0,
                    "data": {"node": gpu.node, "index": gpu.index, "gauges": {}},
                },
            )
            entry["samples"] += 1
            entry["seconds"] += sample.interval_seconds
            data = entry["data"]
            data["product"] = gpu.product
            # Named only when the deployment really is placed here, so a stamp cannot label
            # its devices with another account's deployment.
            served = gpu.deployment_id if gpu.deployment_id in owner else None
            data["deployment_id"] = str(served) if served else None
            _add_gauge(data["gauges"], "util", gpu.utilization_percent)
            _add_gauge(data["gauges"], "mem_used", gpu.memory_used_bytes)
            _add_gauge(data["gauges"], "mem_total", gpu.memory_total_bytes)

    if pending:
        buckets = {key[2] for key in pending}
        existing = {
            (row.kind, row.series, as_utc(row.bucket_start)): row
            for row in (
                await session.execute(
                    select(ServingMetricPoint).where(
                        ServingMetricPoint.stamp_id == stamp_id,
                        ServingMetricPoint.bucket_start.in_(buckets),
                    )
                )
            ).scalars()
        }
        for key, entry in pending.items():
            kind, series, bucket = key
            row = existing.get(key)
            if row is not None and row.account_id != entry["account_id"]:
                # The deployment's account cannot change; a GPU's stamp account cannot
                # either. Start the minute over rather than mix owners in one row.
                await session.delete(row)
                await session.flush()
                row = None
            if row is None:
                session.add(
                    ServingMetricPoint(
                        account_id=entry["account_id"],
                        stamp_id=stamp_id,
                        deployment_id=entry["deployment_id"],
                        kind=kind,
                        series=series,
                        bucket_start=bucket,
                        samples=entry["samples"],
                        seconds=entry["seconds"],
                        data=entry["data"],
                        updated_at=now,
                    )
                )
                continue
            row.samples += entry["samples"]
            row.seconds += entry["seconds"]
            # Reassigned rather than mutated: a JSON column tracks changes by identity.
            row.data = _merge_row_data(kind, dict(row.data or {}), entry["data"])
            row.updated_at = now

    # Not synchronized with the session: the rows just merged are all inside retention, and
    # SQLite hands back naive timestamps that an in-Python evaluation cannot compare.
    await session.execute(
        delete(ServingMetricPoint)
        .where(ServingMetricPoint.stamp_id == stamp_id, ServingMetricPoint.bucket_start < oldest)
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    return IngestResult(
        accepted_at=now,
        accepted_samples=accepted,
        skipped_samples=skipped,
        ignored_deployments=sorted(ignored, key=str),
    )


def _empty_deployment_data() -> dict[str, Any]:
    return {
        "requests": {},
        "input_tokens": 0.0,
        "output_tokens": 0.0,
        "latency": {},
        "ttft": {},
        "itl": {},
        "gauges": {},
        "hosts": 0,
        "gpus": 0,
        "sources": [],
    }


def _merge_row_data(kind: str, current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    gauges = {name: list(value) for name, value in (current.get("gauges") or {}).items()}
    for name, (total, count) in (incoming.get("gauges") or {}).items():
        entry = gauges.setdefault(name, [0.0, 0.0])
        entry[0] += total
        entry[1] += count
    if kind == KIND_GPU:
        return {**current, **{k: v for k, v in incoming.items() if k != "gauges"}, "gauges": gauges}
    merged = _empty_deployment_data()
    for field in ("requests", "latency", "ttft", "itl"):
        merged[field] = dict(current.get(field) or {})
        _merge_counts(merged[field], incoming.get(field))
    for field in ("input_tokens", "output_tokens"):
        merged[field] = float(current.get(field) or 0) + float(incoming.get(field) or 0)
    merged["gauges"] = gauges
    merged["hosts"] = max(int(current.get("hosts") or 0), int(incoming.get("hosts") or 0))
    merged["gpus"] = max(int(current.get("gpus") or 0), int(incoming.get("gpus") or 0))
    merged["sources"] = sorted(
        set(current.get("sources") or []) | set(incoming.get("sources") or [])
    )
    return merged


# --- reads ------------------------------------------------------------------------------


def _bound(key: str) -> float:
    return math.inf if key == "+Inf" else float(key)


def quantile(histogram: dict[str, float], q: float) -> float | None:
    """The q-quantile of a per-bucket histogram, interpolated like histogram_quantile."""
    total = sum(histogram.values())
    if total <= 0:
        return None
    rank = q * total
    seen = 0.0
    lower = 0.0
    largest_finite = 0.0
    for key in sorted(histogram, key=_bound):
        upper = _bound(key)
        count = histogram[key]
        if math.isfinite(upper):
            largest_finite = upper
        if count > 0 and seen + count >= rank:
            if not math.isfinite(upper):
                return largest_finite
            return lower + (upper - lower) * ((rank - seen) / count)
        seen += count
        if math.isfinite(upper):
            lower = upper
    return largest_finite


def _ms(value: float | None) -> float | None:
    return None if value is None else round(value * 1000, 3)


@dataclasses.dataclass
class _Group:
    """One series (one deployment on one stamp, or one GPU) within one chart step."""

    seconds: float = 0.0
    requests: dict[str, float] = dataclasses.field(default_factory=dict)
    input_tokens: float = 0.0
    output_tokens: float = 0.0
    gauges: dict[str, list[float]] = dataclasses.field(default_factory=dict)

    def add(self, row: ServingMetricPoint) -> None:
        data = row.data or {}
        self.seconds += float(row.seconds or 0)
        _merge_counts(self.requests, data.get("requests"))
        self.input_tokens += float(data.get("input_tokens") or 0)
        self.output_tokens += float(data.get("output_tokens") or 0)
        for name, (total, count) in (data.get("gauges") or {}).items():
            entry = self.gauges.setdefault(name, [0.0, 0.0])
            entry[0] += total
            entry[1] += count

    def rate(self, count: float, per: float) -> float:
        return count / self.seconds * per if self.seconds > 0 else 0.0

    def gauge(self, name: str) -> float | None:
        entry = self.gauges.get(name)
        if not entry or entry[1] <= 0:
            return None
        return entry[0] / entry[1]


@dataclasses.dataclass
class _Step:
    groups: dict[tuple[uuid.UUID, str], _Group] = dataclasses.field(
        default_factory=lambda: defaultdict(_Group)
    )
    latency: dict[str, float] = dataclasses.field(default_factory=dict)
    ttft: dict[str, float] = dataclasses.field(default_factory=dict)
    itl: dict[str, float] = dataclasses.field(default_factory=dict)

    def add(self, row: ServingMetricPoint) -> None:
        self.groups[(row.stamp_id, row.series)].add(row)
        data = row.data or {}
        _merge_counts(self.latency, data.get("latency"))
        _merge_counts(self.ttft, data.get("ttft"))
        _merge_counts(self.itl, data.get("itl"))

    @property
    def empty(self) -> bool:
        return not self.groups

    def total_rate(self, pick, per: float) -> float:
        return sum(group.rate(pick(group), per) for group in self.groups.values())

    def status_per_min(self) -> dict[str, float]:
        classes: set[str] = set()
        for group in self.groups.values():
            classes.update(group.requests)
        return {
            name: round(self.total_rate(lambda g, n=name: g.requests.get(n, 0.0), 60), 4)
            for name in sorted(classes)
        }

    def gauge(self, name: str) -> float | None:
        values = [
            value for group in self.groups.values() if (value := group.gauge(name)) is not None
        ]
        if not values:
            return None
        if name in SUMMED_GAUGES:
            return round(sum(values), 4)
        return round(sum(values) / len(values), 4)


class _Grid:
    def __init__(self, window: str, now: dt.datetime) -> None:
        length, step = WINDOWS[window]
        self.window = window
        self.step = step
        self.end = _floor(now, step) + dt.timedelta(seconds=step)
        self.start = self.end - dt.timedelta(seconds=length)
        self.count = length // step
        self.steps = [_Step() for _ in range(self.count)]

    def slot(self, bucket: dt.datetime) -> _Step | None:
        index = int((bucket - self.start).total_seconds() // self.step)
        if 0 <= index < self.count:
            return self.steps[index]
        return None

    def times(self):
        for index, step in enumerate(self.steps):
            yield self.start + dt.timedelta(seconds=index * self.step), step


def _summary(steps: list[_Step]) -> MetricsSummary:
    whole = _Step()
    for step in steps:
        for key, group in step.groups.items():
            target = whole.groups[key]
            target.seconds += group.seconds
            _merge_counts(target.requests, group.requests)
            target.output_tokens += group.output_tokens
        _merge_counts(whole.latency, step.latency)
        _merge_counts(whole.ttft, step.ttft)
    if whole.empty:
        return MetricsSummary()
    requests = sum(sum(group.requests.values()) for group in whole.groups.values())
    errors = sum(
        group.requests.get(name, 0.0) for group in whole.groups.values() for name in ERROR_CLASSES
    )
    output = sum(group.output_tokens for group in whole.groups.values())
    return MetricsSummary(
        requests=round(requests, 3),
        errors=round(errors, 3),
        error_rate=round(errors / requests, 6) if requests > 0 else None,
        requests_per_min=round(whole.total_rate(lambda g: sum(g.requests.values()), 60), 4),
        output_tokens=round(output, 3),
        output_tokens_per_s=round(whole.total_rate(lambda g: g.output_tokens, 1), 4),
        latency_p50_ms=_ms(quantile(whole.latency, 0.5)),
        latency_p95_ms=_ms(quantile(whole.latency, 0.95)),
        ttft_p50_ms=_ms(quantile(whole.ttft, 0.5)),
        ttft_p95_ms=_ms(quantile(whole.ttft, 0.95)),
    )


def _traffic(step: _Step) -> dict[str, Any]:
    if step.empty:
        return {}
    status = step.status_per_min()
    return {
        "requests_per_min": round(sum(status.values()), 4),
        "errors_per_min": round(sum(status.get(name, 0.0) for name in ERROR_CLASSES), 4),
        "status_per_min": status,
        "output_tokens_per_s": round(step.total_rate(lambda g: g.output_tokens, 1), 4),
        "input_tokens_per_s": round(step.total_rate(lambda g: g.input_tokens, 1), 4),
    }


async def _rows(session: AsyncSession, *conditions) -> list[ServingMetricPoint]:
    return list(
        (
            await session.execute(
                select(ServingMetricPoint)
                .where(*conditions)
                .order_by(ServingMetricPoint.bucket_start)
            )
        ).scalars()
    )


def _reported_at(rows: list[ServingMetricPoint]) -> dt.datetime | None:
    stamps = [as_utc(row.updated_at) for row in rows if row.updated_at is not None]
    return max(stamps) if stamps else None


async def deployment_metrics(
    session: AsyncSession, *, account_id: uuid.UUID, deployment_id: uuid.UUID, window: str
) -> DeploymentMetricsResponse:
    """Chart series for one deployment across every stamp it was served from.

    The caller has already confirmed the deployment belongs to ``account_id``; the account
    filter here is the service check, and row-level security repeats it in the database.
    """
    grid = _Grid(window, utc_now())
    rows = await _rows(
        session,
        ServingMetricPoint.account_id == account_id,
        ServingMetricPoint.deployment_id == deployment_id,
        ServingMetricPoint.kind == KIND_DEPLOYMENT,
        ServingMetricPoint.bucket_start >= grid.start,
    )
    sources: set[str] = set()
    for row in rows:
        slot = grid.slot(as_utc(row.bucket_start))
        if slot is not None:
            slot.add(row)
            sources.update((row.data or {}).get("sources") or [])

    points = []
    for moment, step in grid.times():
        point = DeploymentMetricsPoint(t=moment, **_traffic(step))
        if not step.empty:
            point.latency_p50_ms = _ms(quantile(step.latency, 0.5))
            point.latency_p95_ms = _ms(quantile(step.latency, 0.95))
            point.ttft_p50_ms = _ms(quantile(step.ttft, 0.5))
            point.ttft_p95_ms = _ms(quantile(step.ttft, 0.95))
            point.itl_p50_ms = _ms(quantile(step.itl, 0.5))
            point.running = step.gauge("running")
            point.waiting = step.gauge("waiting")
            point.kv_cache_percent = step.gauge("kv")
            point.gpu_utilization_percent = step.gauge("gpu_util")
            point.gpu_memory_used_bytes = step.gauge("gpu_mem_used")
            point.gpu_memory_total_bytes = step.gauge("gpu_mem_total")
        points.append(point)

    return DeploymentMetricsResponse(
        deployment_id=deployment_id,
        window=window,
        step_seconds=grid.step,
        start=grid.start,
        end=grid.end,
        reported_at=_reported_at(rows),
        sources=sorted(sources),
        summary=_summary(grid.steps),
        points=points,
    )


async def stamp_metrics(
    session: AsyncSession, *, account_id: uuid.UUID, stamp_id: uuid.UUID, window: str
) -> StampMetricsResponse:
    """Per-GPU series and stamp totals for a stamp the account owns."""
    stamp = (
        await session.execute(
            select(InferenceStamp).where(
                InferenceStamp.id == stamp_id, InferenceStamp.account_id == account_id
            )
        )
    ).scalar_one_or_none()
    if stamp is None:
        raise NotFound("stamp_not_found", "Inference stamp does not exist")

    now = utc_now()
    traffic = _Grid(window, now)
    rows = await _rows(
        session,
        ServingMetricPoint.account_id == account_id,
        ServingMetricPoint.stamp_id == stamp_id,
        ServingMetricPoint.bucket_start >= traffic.start,
    )
    devices: dict[str, _Grid] = {}
    latest_row: dict[str, ServingMetricPoint] = {}
    sources: set[str] = set()
    for row in rows:
        bucket = as_utc(row.bucket_start)
        if row.kind == KIND_GPU:
            device = devices.setdefault(row.series, _Grid(window, now))
            slot = device.slot(bucket)
            if slot is not None:
                slot.add(row)
                latest_row[row.series] = row
                sources.add("dcgm")
        else:
            slot = traffic.slot(bucket)
            if slot is not None:
                slot.add(row)
                sources.update((row.data or {}).get("sources") or [])

    gpus: list[GPUSeries] = []
    for series in sorted(devices, key=_device_order):
        device = devices[series]
        points = []
        latest: GPUSeriesPoint | None = None
        for moment, step in device.times():
            point = GPUSeriesPoint(t=moment)
            if not step.empty:
                point.utilization_percent = step.gauge("util")
                point.memory_used_bytes = step.gauge("mem_used")
                point.memory_total_bytes = step.gauge("mem_total")
                latest = point
            points.append(point)
        data = latest_row[series].data or {}
        served = data.get("deployment_id")
        gpus.append(
            GPUSeries(
                node=str(data.get("node") or series.rsplit("/", 1)[0]),
                index=str(data.get("index") or series.rsplit("/", 1)[-1]),
                product=data.get("product"),
                deployment_id=uuid.UUID(served) if served else None,
                latest=latest,
                points=points,
            )
        )

    totals: list[StampMetricsPoint] = []
    for index, (moment, step) in enumerate(traffic.times()):
        point = StampMetricsPoint(t=moment)
        utils = [
            value
            for device in devices.values()
            if (value := device.steps[index].gauge("util")) is not None
        ]
        if utils:
            point.gpu_utilization_percent = round(sum(utils) / len(utils), 4)
            point.gpu_memory_used_bytes = round(
                sum(device.steps[index].gauge("mem_used") or 0 for device in devices.values()), 1
            )
            point.gpu_memory_total_bytes = round(
                sum(device.steps[index].gauge("mem_total") or 0 for device in devices.values()), 1
            )
        if not step.empty:
            point.requests_per_min = round(
                step.total_rate(lambda g: sum(g.requests.values()), 60), 4
            )
            point.output_tokens_per_s = round(step.total_rate(lambda g: g.output_tokens, 1), 4)
        totals.append(point)

    return StampMetricsResponse(
        stamp_id=stamp_id,
        window=window,
        step_seconds=traffic.step,
        start=traffic.start,
        end=traffic.end,
        reported_at=_reported_at(rows),
        sources=sorted(sources),
        totals=totals,
        gpus=gpus,
    )


def _device_order(series: str) -> tuple[str, int, str]:
    node, _, index = series.rpartition("/")
    return (node, int(index) if index.isdigit() else 0, index)


async def account_metrics(
    session: AsyncSession, *, account_id: uuid.UUID, window: str
) -> AccountMetricsResponse:
    """Request rate and throughput across every deployment of the account."""
    grid = _Grid(window, utc_now())
    rows = await _rows(
        session,
        ServingMetricPoint.account_id == account_id,
        ServingMetricPoint.kind == KIND_DEPLOYMENT,
        ServingMetricPoint.bucket_start >= grid.start,
    )
    for row in rows:
        slot = grid.slot(as_utc(row.bucket_start))
        if slot is not None:
            slot.add(row)
    points = []
    for moment, step in grid.times():
        traffic = _traffic(step)
        points.append(
            AccountMetricsPoint(
                t=moment,
                requests_per_min=traffic.get("requests_per_min"),
                errors_per_min=traffic.get("errors_per_min"),
                output_tokens_per_s=traffic.get("output_tokens_per_s"),
            )
        )
    return AccountMetricsResponse(
        window=window,
        step_seconds=grid.step,
        start=grid.start,
        end=grid.end,
        reported_at=_reported_at(rows),
        summary=_summary(grid.steps),
        points=points,
    )


async def count_points(session: AsyncSession) -> int:
    """Rows currently stored; for tests and operational checks."""
    return (await session.execute(select(func.count(ServingMetricPoint.id)))).scalar_one()
