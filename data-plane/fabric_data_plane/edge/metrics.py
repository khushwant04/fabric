"""Prometheus metrics for the edge, in the same hand-written style as the gateway's.

Labelled by stamp name and outcome only. Account ids and model names are deliberately not
labels: the edge serves every account from one process, and per-tenant series belong on
the stamps, which already meter and label by account.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable

_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0)


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class EdgeMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str], int] = defaultdict(int)
        self._buckets: dict[str, list[int]] = defaultdict(lambda: [0] * (len(_LATENCY_BUCKETS) + 1))
        self._latency_sum: dict[str, float] = defaultdict(float)
        self._latency_count: dict[str, int] = defaultdict(int)
        self._ejections: dict[str, int] = defaultdict(int)
        self._retries: dict[str, int] = defaultdict(int)
        self._table_fetches: dict[str, int] = defaultdict(int)
        self._started = time.time()

    def request(self, *, stamp: str, outcome: str, seconds: float | None = None) -> None:
        with self._lock:
            self._requests[(stamp, outcome)] += 1
            if seconds is not None:
                buckets = self._buckets[stamp]
                for index, bound in enumerate(_LATENCY_BUCKETS):
                    if seconds <= bound:
                        buckets[index] += 1
                buckets[-1] += 1
                self._latency_sum[stamp] += seconds
                self._latency_count[stamp] += 1

    def ejected(self, stamp: str) -> None:
        with self._lock:
            self._ejections[stamp] += 1

    def retried(self, stamp: str) -> None:
        with self._lock:
            self._retries[stamp] += 1

    def table_fetch(self, outcome: str) -> None:
        with self._lock:
            self._table_fetches[outcome] += 1

    def render(
        self,
        *,
        table_age: float | None,
        table_revision: int,
        table_from_disk: bool,
        keys_held: int,
        stamp_states: dict[str, dict],
        stamp_names: Callable[[str], str],
    ) -> str:
        with self._lock:
            requests = dict(self._requests)
            buckets = {key: list(value) for key, value in self._buckets.items()}
            sums = dict(self._latency_sum)
            counts = dict(self._latency_count)
            ejections = dict(self._ejections)
            retries = dict(self._retries)
            fetches = dict(self._table_fetches)
            uptime = time.time() - self._started

        lines = [
            "# HELP fabric_edge_requests_total Requests handled by the edge, by stamp and outcome.",
            "# TYPE fabric_edge_requests_total counter",
        ]
        for (stamp, outcome), value in sorted(requests.items()):
            lines.append(
                f'fabric_edge_requests_total{{stamp="{_label(stamp)}",outcome="{outcome}"}} {value}'
            )
        lines += [
            "# HELP fabric_edge_request_duration_seconds Time from receipt to the end of the "
            "response body, by stamp.",
            "# TYPE fabric_edge_request_duration_seconds histogram",
        ]
        for stamp, values in sorted(buckets.items()):
            label = _label(stamp)
            for bound, value in zip(_LATENCY_BUCKETS, values, strict=False):
                lines.append(
                    f'fabric_edge_request_duration_seconds_bucket{{stamp="{label}",le="{bound}"}} '
                    f"{value}"
                )
            lines.append(
                f'fabric_edge_request_duration_seconds_bucket{{stamp="{label}",le="+Inf"}} '
                f"{values[-1]}"
            )
            lines.append(
                f'fabric_edge_request_duration_seconds_sum{{stamp="{label}"}} {sums[stamp]}'
            )
            lines.append(
                f'fabric_edge_request_duration_seconds_count{{stamp="{label}"}} {counts[stamp]}'
            )
        lines += [
            "# HELP fabric_edge_stamp_ejections_total Times a stamp was ejected after "
            "connection failures.",
            "# TYPE fabric_edge_stamp_ejections_total counter",
        ]
        for stamp, value in sorted(ejections.items()):
            lines.append(f'fabric_edge_stamp_ejections_total{{stamp="{_label(stamp)}"}} {value}')
        lines += [
            "# HELP fabric_edge_retries_total Requests moved to another stamp after a "
            "connection failure on this one.",
            "# TYPE fabric_edge_retries_total counter",
        ]
        for stamp, value in sorted(retries.items()):
            lines.append(f'fabric_edge_retries_total{{stamp="{_label(stamp)}"}} {value}')
        lines += [
            "# HELP fabric_edge_stamp_in_flight Requests this edge has in flight to a stamp.",
            "# TYPE fabric_edge_stamp_in_flight gauge",
        ]
        for stamp_id, state in sorted(stamp_states.items()):
            name = _label(stamp_names(stamp_id))
            lines.append(f'fabric_edge_stamp_in_flight{{stamp="{name}"}} {state["in_flight"]}')
        lines += [
            "# HELP fabric_edge_stamp_available Whether a stamp is currently eligible "
            "(1) or ejected (0).",
            "# TYPE fabric_edge_stamp_available gauge",
        ]
        for stamp_id, state in sorted(stamp_states.items()):
            name = _label(stamp_names(stamp_id))
            lines.append(
                f'fabric_edge_stamp_available{{stamp="{name}"}} {0 if state["ejected"] else 1}'
            )
        lines += [
            "# HELP fabric_edge_routing_table_fetches_total Routing-table polls, by outcome.",
            "# TYPE fabric_edge_routing_table_fetches_total counter",
        ]
        for outcome, value in sorted(fetches.items()):
            lines.append(f'fabric_edge_routing_table_fetches_total{{outcome="{outcome}"}} {value}')
        lines += [
            "# HELP fabric_edge_routing_table_age_seconds Seconds since the control plane last "
            "confirmed the table in force (-1 when there is none).",
            "# TYPE fabric_edge_routing_table_age_seconds gauge",
            "fabric_edge_routing_table_age_seconds "
            f"{-1 if table_age is None else round(table_age, 3)}",
            "# HELP fabric_edge_routing_table_revision Revision of the table in force.",
            "# TYPE fabric_edge_routing_table_revision gauge",
            f"fabric_edge_routing_table_revision {table_revision}",
            "# HELP fabric_edge_routing_table_from_disk Whether the table in force was loaded "
            "from the on-disk cache and not yet confirmed.",
            "# TYPE fabric_edge_routing_table_from_disk gauge",
            f"fabric_edge_routing_table_from_disk {int(table_from_disk)}",
            "# HELP fabric_edge_verification_keys Verification keys held.",
            "# TYPE fabric_edge_verification_keys gauge",
            f"fabric_edge_verification_keys {keys_held}",
            "# HELP fabric_edge_uptime_seconds Seconds since the edge started.",
            "# TYPE fabric_edge_uptime_seconds gauge",
            f"fabric_edge_uptime_seconds {round(uptime, 3)}",
        ]
        return "\n".join(lines) + "\n"
