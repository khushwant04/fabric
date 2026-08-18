"""Prometheus metrics for the gateway's own view of traffic.

The model server publishes engine metrics of its own, and they are not a substitute for
these. A request refused here for an expired token, an unowned deployment, or a
concurrency cap never reaches a model host, so it appears in no engine metric: from the
engine's side that traffic did not happen, while from the caller's side it did and failed.
Only the gateway can report it.

Written by hand rather than pulled from a client library. The exposition format is a few
lines of text, the data is already being counted, and a serving component that is
deliberately dependency-light should not gain a dependency to print numbers it holds.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

#: Upper bounds in seconds. Chosen for a language model rather than a web service: the
#: interesting range is hundreds of milliseconds to tens of seconds, and a bucket at 5ms
#: would only ever count refusals.
_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)


class Metrics:
    """Counters and latency histograms, labelled by deployment and outcome."""

    def __init__(self) -> None:
        # One lock, because a request touches several series at once and a scrape that
        # saw half of an update would report a total that never existed.
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self._tokens: dict[tuple[str, str], int] = defaultdict(int)
        self._latency_buckets: dict[str, list[int]] = defaultdict(
            lambda: [0] * (len(_LATENCY_BUCKETS) + 1)
        )
        self._latency_sum: dict[str, float] = defaultdict(float)
        self._latency_count: dict[str, int] = defaultdict(int)
        self._in_flight: dict[str, int] = defaultdict(int)
        self._started = time.time()

    def request_started(self, deployment: str) -> None:
        with self._lock:
            self._in_flight[deployment] += 1

    def request_finished(
        self,
        *,
        deployment: str,
        account: str,
        outcome: str,
        duration_seconds: float | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """Record one completed request, however it ended."""
        with self._lock:
            if self._in_flight.get(deployment, 0) > 0:
                self._in_flight[deployment] -= 1
            self._requests[(deployment, account, outcome)] += 1

            if duration_seconds is not None:
                index = len(_LATENCY_BUCKETS)
                for position, bound in enumerate(_LATENCY_BUCKETS):
                    if duration_seconds <= bound:
                        index = position
                        break
                buckets = self._latency_buckets[deployment]
                # Cumulative, as the format requires: each bucket counts everything at or
                # below its bound.
                for position in range(index, len(buckets)):
                    buckets[position] += 1
                self._latency_sum[deployment] += duration_seconds
                self._latency_count[deployment] += 1

            if input_tokens:
                self._tokens[(deployment, "input")] += input_tokens
            if output_tokens:
                self._tokens[(deployment, "output")] += output_tokens

    def refused(self, *, deployment: str, account: str, reason: str) -> None:
        """Record a request rejected before it reached a model host."""
        with self._lock:
            self._requests[(deployment, account, reason)] += 1

    def render(self) -> str:
        """Return the metrics in Prometheus text exposition format."""
        with self._lock:
            requests = dict(self._requests)
            tokens = dict(self._tokens)
            buckets = {name: list(values) for name, values in self._latency_buckets.items()}
            sums = dict(self._latency_sum)
            counts = dict(self._latency_count)
            in_flight = dict(self._in_flight)
            uptime = time.time() - self._started

        lines: list[str] = []

        lines.append("# HELP fabric_dp_requests_total Requests handled, by outcome.")
        lines.append("# TYPE fabric_dp_requests_total counter")
        for (deployment, account, outcome), value in sorted(requests.items()):
            lines.append(
                f'fabric_dp_requests_total{{deployment_id="{deployment}",'
                f'account_id="{account}",outcome="{outcome}"}} {value}'
            )

        lines.append("# HELP fabric_dp_requests_in_flight Requests currently upstream.")
        lines.append("# TYPE fabric_dp_requests_in_flight gauge")
        for deployment, value in sorted(in_flight.items()):
            lines.append(f'fabric_dp_requests_in_flight{{deployment_id="{deployment}"}} {value}')

        lines.append("# HELP fabric_dp_tokens_total Tokens the model reported, by direction.")
        lines.append("# TYPE fabric_dp_tokens_total counter")
        for (deployment, direction), value in sorted(tokens.items()):
            lines.append(
                f'fabric_dp_tokens_total{{deployment_id="{deployment}",'
                f'direction="{direction}"}} {value}'
            )

        lines.append("# HELP fabric_dp_request_duration_seconds End-to-end gateway latency.")
        lines.append("# TYPE fabric_dp_request_duration_seconds histogram")
        for deployment, values in sorted(buckets.items()):
            for position, bound in enumerate(_LATENCY_BUCKETS):
                lines.append(
                    f'fabric_dp_request_duration_seconds_bucket{{deployment_id="{deployment}",'
                    f'le="{bound}"}} {values[position]}'
                )
            lines.append(
                f'fabric_dp_request_duration_seconds_bucket{{deployment_id="{deployment}",'
                f'le="+Inf"}} {values[-1]}'
            )
            lines.append(
                f'fabric_dp_request_duration_seconds_sum{{deployment_id="{deployment}"}} '
                f"{sums.get(deployment, 0.0)}"
            )
            lines.append(
                f'fabric_dp_request_duration_seconds_count{{deployment_id="{deployment}"}} '
                f"{counts.get(deployment, 0)}"
            )

        lines.append("# HELP fabric_dp_uptime_seconds Seconds since this process started.")
        lines.append("# TYPE fabric_dp_uptime_seconds gauge")
        lines.append(f"fabric_dp_uptime_seconds {uptime:.3f}")

        return "\n".join(lines) + "\n"
