"""Serving-metric points: what a deployment's traffic and a stamp's GPUs did, per minute.

Kept beside ``app.models`` rather than in it so the time-series table stays self-contained;
it shares the same declarative ``Base``, so migrations, metadata and the test fixtures see
it like any other table. See ``app.services.serving_metrics`` and docs/serving-metrics.md.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, JsonColumn
from app.core.timeutil import utc_now
from app.models import _uuid_pk

#: A deployment's traffic on one stamp, owned by the deployment's account.
KIND_DEPLOYMENT = "deployment"
#: One GPU device on a stamp, owned by the stamp's account.
KIND_GPU = "gpu"


class ServingMetricPoint(Base):
    """One minute of one series on one stamp.

    Samples arriving within the same minute are merged into its row: counts and histogram
    buckets add, gauges keep a running sum and sample count. ``seconds`` is how much of the
    minute the merged samples cover, which is what rates are divided by. Rows older than
    the retention period are deleted as new samples arrive.
    """

    __tablename__ = "serving_metric_points"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stamp_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("inference_stamps.id", ondelete="CASCADE"), nullable=False
    )
    #: Set for deployment rows; a GPU row names the deployment it served inside ``data``.
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The deployment id for a deployment row, ``node/index`` for a GPU row.
    series: Mapped[str] = mapped_column(String(300), nullable=False)
    bucket_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    samples: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    data: Mapped[dict[str, Any]] = mapped_column(JsonColumn, nullable=False, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "deployment_id"],
            ["deployments.account_id", "deployments.id"],
            ondelete="CASCADE",
            name="fk_serving_metric_points_deployment",
        ),
        CheckConstraint("kind in ('deployment', 'gpu')", name="serving_metric_points_kind"),
        UniqueConstraint(
            "stamp_id", "kind", "series", "bucket_start", name="uq_serving_metric_points_bucket"
        ),
        Index(
            "ix_serving_metric_points_deployment",
            "account_id",
            "deployment_id",
            "bucket_start",
        ),
        Index("ix_serving_metric_points_stamp", "stamp_id", "bucket_start"),
    )
