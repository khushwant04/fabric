"""Request and response models for serving metrics (docs/serving-metrics.md).

Kept beside ``app.schemas`` so the metrics contract reads as one unit. The request side
mirrors ``agent/internal/servingmetrics``; unknown fields are rejected and every collection
is bounded, like the host report.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Bounds shared with the agent.
MAX_SAMPLES = 20
MAX_DEPLOYMENTS = 64
MAX_GPUS = 128
MAX_BUCKETS = 64

STATUS_CLASSES = ("2xx", "4xx", "5xx", "cancelled")
SOURCES = ("gateway", "model_host", "dcgm")

Window = Literal["1h", "6h", "24h", "7d"]


def _histogram(value: dict[str, float] | None) -> dict[str, float] | None:
    if value is None:
        return None
    if len(value) > MAX_BUCKETS:
        raise ValueError(f"a histogram has at most {MAX_BUCKETS} buckets")
    cleaned: dict[str, float] = {}
    for bound, count in value.items():
        if bound != "+Inf":
            try:
                parsed = float(bound)
            except ValueError as exc:
                raise ValueError(f"bucket bound {bound!r} is not a number") from exc
            if not math.isfinite(parsed) or parsed < 0:
                raise ValueError(f"bucket bound {bound!r} is out of range")
        if not math.isfinite(count) or count < 0:
            raise ValueError("bucket counts are non-negative")
        if count:
            cleaned[bound] = count
    return cleaned


class DeploymentMetricPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_id: uuid.UUID
    requests: dict[Literal["2xx", "4xx", "5xx", "cancelled"], float] = Field(default_factory=dict)
    input_tokens: float = Field(default=0, ge=0, allow_inf_nan=False)
    output_tokens: float = Field(default=0, ge=0, allow_inf_nan=False)
    latency_seconds: dict[str, float] | None = None
    ttft_seconds: dict[str, float] | None = None
    itl_seconds: dict[str, float] | None = None
    hosts: int = Field(default=0, ge=0, le=1024)
    running: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    waiting: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    kv_cache_percent: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    gpus: int = Field(default=0, ge=0, le=MAX_GPUS)
    gpu_utilization_percent: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    gpu_memory_used_bytes: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    gpu_memory_total_bytes: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("requests")
    @classmethod
    def _counts(cls, value: dict[str, float]) -> dict[str, float]:
        for count in value.values():
            if not math.isfinite(count) or count < 0:
                raise ValueError("request counts are non-negative")
        return value

    @field_validator("latency_seconds", "ttft_seconds", "itl_seconds")
    @classmethod
    def _buckets(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        return _histogram(value)


class GPUMetricPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str = Field(max_length=253)
    index: str = Field(min_length=1, max_length=16)
    product: str | None = Field(default=None, max_length=128)
    utilization_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    memory_used_bytes: float = Field(ge=0, allow_inf_nan=False)
    memory_total_bytes: float = Field(ge=0, allow_inf_nan=False)
    deployment_id: uuid.UUID | None = None


class MetricSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: dt.datetime
    interval_seconds: float = Field(gt=0, le=3600, allow_inf_nan=False)
    sources: list[Literal["gateway", "model_host", "dcgm"]] = Field(
        default_factory=list, max_length=3
    )
    deployments: list[DeploymentMetricPoint] = Field(
        default_factory=list, max_length=MAX_DEPLOYMENTS
    )
    gpus: list[GPUMetricPoint] = Field(default_factory=list, max_length=MAX_GPUS)


class MetricReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    samples: list[MetricSample] = Field(min_length=1, max_length=MAX_SAMPLES)


class MetricReportResponse(BaseModel):
    stamp_id: uuid.UUID
    accepted_at: dt.datetime
    accepted_samples: int
    #: Samples outside the retention window or too far in the future.
    skipped_samples: int
    #: Deployments named in the report that are not placed on this stamp.
    ignored_deployments: list[uuid.UUID]


# --- reads ------------------------------------------------------------------------------


class DeploymentMetricsPoint(BaseModel):
    """One chart step. ``None`` means no stamp reported during the step."""

    t: dt.datetime
    requests_per_min: float | None = None
    errors_per_min: float | None = None
    status_per_min: dict[str, float] | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None
    itl_p50_ms: float | None = None
    output_tokens_per_s: float | None = None
    input_tokens_per_s: float | None = None
    running: float | None = None
    waiting: float | None = None
    kv_cache_percent: float | None = None
    gpu_utilization_percent: float | None = None
    gpu_memory_used_bytes: float | None = None
    gpu_memory_total_bytes: float | None = None


class MetricsSummary(BaseModel):
    """Whole-window figures for the headline numbers above the charts."""

    requests: float = 0
    errors: float = 0
    error_rate: float | None = None
    requests_per_min: float | None = None
    output_tokens: float = 0
    output_tokens_per_s: float | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None


class DeploymentMetricsResponse(BaseModel):
    deployment_id: uuid.UUID
    window: Window
    step_seconds: int
    start: dt.datetime
    end: dt.datetime
    #: The newest minute any stamp reported for this deployment, or ``None`` if none has.
    reported_at: dt.datetime | None
    sources: list[str]
    summary: MetricsSummary
    points: list[DeploymentMetricsPoint]


class StampMetricsPoint(BaseModel):
    t: dt.datetime
    gpu_utilization_percent: float | None = None
    gpu_memory_used_bytes: float | None = None
    gpu_memory_total_bytes: float | None = None
    requests_per_min: float | None = None
    output_tokens_per_s: float | None = None


class GPUSeriesPoint(BaseModel):
    t: dt.datetime
    utilization_percent: float | None = None
    memory_used_bytes: float | None = None
    memory_total_bytes: float | None = None


class GPUSeries(BaseModel):
    node: str
    index: str
    product: str | None
    deployment_id: uuid.UUID | None
    latest: GPUSeriesPoint | None
    points: list[GPUSeriesPoint]


class StampMetricsResponse(BaseModel):
    stamp_id: uuid.UUID
    window: Window
    step_seconds: int
    start: dt.datetime
    end: dt.datetime
    reported_at: dt.datetime | None
    sources: list[str]
    totals: list[StampMetricsPoint]
    gpus: list[GPUSeries]


class AccountMetricsPoint(BaseModel):
    t: dt.datetime
    requests_per_min: float | None = None
    errors_per_min: float | None = None
    output_tokens_per_s: float | None = None


class AccountMetricsResponse(BaseModel):
    """Every deployment of the account together, for the overview's sparklines."""

    window: Window
    step_seconds: int
    start: dt.datetime
    end: dt.datetime
    reported_at: dt.datetime | None
    summary: MetricsSummary
    points: list[AccountMetricsPoint]
