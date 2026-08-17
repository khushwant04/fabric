"""Stamp metrics.

Usage answers what an account owes. Metrics answer whether a stamp is healthy, which is a
different question with different storage needs: many samples per minute, interesting in
aggregate, worthless individually after a day.

They are deliberately not stored as rows. A table would be a time-series database with no
retention policy, no downsampling, and no query language, and it would grow without bound
while being awkward to read. Fabric has no metrics store yet, so the latest sample is kept
on the stamp's own state and the rest is logged. That keeps the ingestion path real and
gives an operator something to look at, without pretending a relational table is a metrics
system.

The shape of what is kept is chosen so a store can be added later without changing what
stamps send.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InferenceStamp

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class RecordedMetrics:
    gpus_recorded: int
    runtime_recorded: bool


def summarize(gpus: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce per-device samples to the few numbers a stamp view needs.

    The maximum rather than the mean for utilisation and temperature: one saturated device
    is the interesting fact on a stamp, and averaging it with idle ones hides exactly the
    condition worth seeing.
    """
    if not gpus:
        return {}

    def highest(field: str) -> float:
        return max(float(sample.get(field) or 0) for sample in gpus)

    used = sum(float(sample.get("memory_used_mib") or 0) for sample in gpus)
    total = sum(float(sample.get("memory_total_mib") or 0) for sample in gpus)

    summary = {
        "devices": len(gpus),
        "max_utilization_gpu_percent": highest("utilization_gpu_percent"),
        "max_temperature_c": highest("temperature_c"),
        "memory_used_mib": used,
        "memory_total_mib": total,
    }

    clocks = [
        float(sample["sm_clock_mhz"]) / float(sample["sm_clock_max_mhz"])
        for sample in gpus
        if float(sample.get("sm_clock_max_mhz") or 0) > 0
    ]
    if clocks:
        # Lowest fraction across devices, since a single throttled device limits the
        # deployment placed on it. Reported as a ratio rather than as "throttling",
        # because the clock alone cannot say whether the cause is idleness, power, or
        # heat.
        summary["min_sm_clock_fraction"] = min(clocks)
    return summary


async def record_metrics(
    session: AsyncSession,
    *,
    stamp: InferenceStamp,
    collected_at: dt.datetime,
    gpus: list[dict[str, Any]],
    runtime: dict[str, Any],
) -> RecordedMetrics:
    """Attach the latest sample to the stamp and log the rest."""
    summary = summarize(gpus)

    capabilities = dict(stamp.capabilities or {})
    capabilities["metrics"] = {
        "collected_at": collected_at.isoformat(),
        "gpu": summary,
        "runtime": {
            "reachable": bool(runtime.get("reachable")),
            "values": runtime.get("values") or {},
            "unreachable_cause": runtime.get("unreachable_cause"),
        },
    }
    # Reassigned rather than mutated in place: a JSON column tracks changes by identity,
    # and mutating the existing dict would leave the write invisible to the session.
    stamp.capabilities = capabilities
    await session.flush()

    logger.info(
        "stamp %s metrics: devices=%s max_util=%s runtime_reachable=%s",
        stamp.id,
        summary.get("devices", 0),
        summary.get("max_utilization_gpu_percent"),
        bool(runtime.get("reachable")),
    )

    return RecordedMetrics(
        gpus_recorded=len(gpus), runtime_recorded=bool(runtime.get("reachable"))
    )
