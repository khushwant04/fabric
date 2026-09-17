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

from fabric_data_plane.streaming import UNMETERED_REASONS

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
        # Per-backend series, so a balancing strategy's effect is measurable: which
        # backend served how many requests with what outcome, how many are in flight on
        # each, and how often each was ejected. Labelled by backend in addition to
        # deployment, because a deployment is now a pool (ADR 0010) and a single
        # deployment-level number would hide an uneven spread or one hot host.
        self._backend_requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self._backend_in_flight: dict[tuple[str, str], int] = defaultdict(int)
        self._backend_ejections: dict[tuple[str, str], int] = defaultdict(int)
        self._backend_available: dict[tuple[str, str], int] = {}
        self._active_backends: set[tuple[str, str]] = set()
        # Streamed requests that ended without a usable token count, so no usage record was
        # written. Counted rather than recorded as zero, because a zero-token row is
        # indistinguishable from a real answer that cost nothing. Labelled by account, since
        # lost usage is lost revenue belonging to somebody, and by reason, since the causes
        # have different owners. Seeded to zero per identity so that the *first* loss is a
        # change an alert can fire on rather than a series appearing from nowhere.
        self._unmetered_streams: dict[tuple[str, str, str], int] = defaultdict(int)
        self._active_deployments: set[tuple[str, str]] = set()
        self._started = time.time()

    def activate_deployment(self, *, deployment: str, account: str) -> None:
        """Declare a placement and publish zero loss counters before it serves traffic."""
        with self._lock:
            self._active_deployments.add((deployment, account))
            for reason in UNMETERED_REASONS:
                self._unmetered_streams.setdefault((deployment, account, reason), 0)

    def request_started(self, deployment: str) -> None:
        with self._lock:
            self._in_flight[deployment] += 1

    def activate_backend(
        self, *, deployment: str, backend: str, available: bool
    ) -> None:
        """Declare a current pool member and seed its availability gauge."""
        with self._lock:
            key = (deployment, backend)
            self._active_backends.add(key)
            self._backend_available[key] = int(available)

    def backend_started(self, *, deployment: str, backend: str) -> None:
        """Record that a request has been dispatched to a current backend."""
        with self._lock:
            if (deployment, backend) not in self._active_backends:
                return
            self._backend_in_flight[(deployment, backend)] += 1

    def backend_finished(
        self, *, deployment: str, backend: str, outcome: str
    ) -> None:
        """Record that a request to a specific backend has ended, with its outcome."""
        with self._lock:
            if (deployment, backend) not in self._active_backends:
                return
            if self._backend_in_flight.get((deployment, backend), 0) > 0:
                self._backend_in_flight[(deployment, backend)] -= 1
            self._backend_requests[(deployment, backend, outcome)] += 1

    def backend_ejected(self, *, deployment: str, backend: str) -> None:
        """Record that a backend was ejected from a pool after crossing the threshold."""
        with self._lock:
            if (deployment, backend) not in self._active_backends:
                return
            self._backend_ejections[(deployment, backend)] += 1

    def backend_health(self, *, deployment: str, backend: str, available: bool) -> None:
        """Set current backend eligibility after health cooldown/ejection changes."""
        with self._lock:
            if (deployment, backend) not in self._active_backends:
                return
            self._backend_available[(deployment, backend)] = int(available)

    def retire_backend(self, *, deployment: str, backend: str) -> None:
        """Remove every series for a backend no longer present in the routing pool."""
        with self._lock:
            self._active_backends.discard((deployment, backend))
            self._backend_in_flight.pop((deployment, backend), None)
            self._backend_ejections.pop((deployment, backend), None)
            self._backend_available.pop((deployment, backend), None)
            for key in [
                key
                for key in self._backend_requests
                if key[0] == deployment and key[1] == backend
            ]:
                del self._backend_requests[key]

    def retire_deployment(self, deployment: str) -> None:
        """Retire current membership while retaining process-lifetime counter history."""
        with self._lock:
            self._active_deployments = {
                key for key in self._active_deployments if key[0] != deployment
            }
            self._active_backends = {
                key for key in self._active_backends if key[0] != deployment
            }
            for mapping in (
                self._backend_requests,
                self._backend_in_flight,
                self._backend_ejections,
                self._backend_available,
            ):
                for key in [key for key in mapping if key[0] == deployment]:
                    del mapping[key]

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

    def stream_finished(
        self, *, deployment: str, account: str, unmetered_reason: str | None
    ) -> None:
        """Record one streamed request, and whether its token count was lost.

        The zero baseline is published by ``activate_deployment`` when the registry first makes
        the placement known, before traffic can finish. This method never creates a label: a
        stream admitted before withdrawal retains membership through this call, while an
        unretained late cleanup cannot add a new event after retirement.
        """
        with self._lock:
            if (deployment, account) not in self._active_deployments:
                return
            if unmetered_reason is not None:
                self._unmetered_streams[(deployment, account, unmetered_reason)] += 1

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
            backend_requests = dict(self._backend_requests)
            backend_in_flight = dict(self._backend_in_flight)
            backend_ejections = dict(self._backend_ejections)
            backend_available = dict(self._backend_available)
            unmetered_streams = dict(self._unmetered_streams)
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

        lines.append(
            "# HELP fabric_dp_backend_requests_total Requests per backend, by outcome."
        )
        lines.append("# TYPE fabric_dp_backend_requests_total counter")
        for (deployment, backend, outcome), value in sorted(backend_requests.items()):
            lines.append(
                f'fabric_dp_backend_requests_total{{deployment_id="{deployment}",'
                f'backend_id="{backend}",outcome="{outcome}"}} {value}'
            )

        lines.append(
            "# HELP fabric_dp_backend_in_flight Requests currently in flight per backend."
        )
        lines.append("# TYPE fabric_dp_backend_in_flight gauge")
        for (deployment, backend), value in sorted(backend_in_flight.items()):
            lines.append(
                f'fabric_dp_backend_in_flight{{deployment_id="{deployment}",'
                f'backend_id="{backend}"}} {value}'
            )

        lines.append(
            "# HELP fabric_dp_backend_ejections_total Times a backend was ejected from a pool."
        )
        lines.append("# TYPE fabric_dp_backend_ejections_total counter")
        for (deployment, backend), value in sorted(backend_ejections.items()):
            lines.append(
                f'fabric_dp_backend_ejections_total{{deployment_id="{deployment}",'
                f'backend_id="{backend}"}} {value}'
            )

        lines.append(
            "# HELP fabric_dp_backend_available Whether a backend is eligible for routing."
        )
        lines.append("# TYPE fabric_dp_backend_available gauge")
        for (deployment, backend), value in sorted(backend_available.items()):
            lines.append(
                f'fabric_dp_backend_available{{deployment_id="{deployment}",'
                f'backend_id="{backend}"}} {value}'
            )

        lines.append("# HELP fabric_dp_tokens_total Tokens the model reported, by direction.")
        lines.append("# TYPE fabric_dp_tokens_total counter")
        for (deployment, direction), value in sorted(tokens.items()):
            lines.append(
                f'fabric_dp_tokens_total{{deployment_id="{deployment}",'
                f'direction="{direction}"}} {value}'
            )

        lines.append(
            "# HELP fabric_dp_unmetered_streams_total "
            "Streamed requests whose token count was lost, by reason."
        )
        lines.append("# TYPE fabric_dp_unmetered_streams_total counter")
        for (deployment, account, reason), value in sorted(unmetered_streams.items()):
            lines.append(
                f'fabric_dp_unmetered_streams_total{{deployment_id="{deployment}",'
                f'account_id="{account}",reason="{reason}"}} {value}'
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
