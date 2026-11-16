"""Prometheus series for hosted tools, rendered by :class:`fabric_data_plane.metrics.Metrics`.

Labels are the tool name and a bounded outcome (``ok``, ``code_error``, ``timeout``,
``error``) or byte kind; never a URL, query or account.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any


def outcome_of(ok: bool, error: str | None, details: dict[str, Any]) -> str:
    if error and ("timeout" in error):
        return "timeout"
    if not ok:
        return "error"
    if details.get("exit_code") not in (None, 0):
        return "code_error"
    return "ok"


class HostedToolMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executions: dict[tuple[str, str], int] = defaultdict(int)
        self._duration_sum: dict[str, float] = defaultdict(float)
        self._bytes: dict[tuple[str, str], int] = defaultdict(int)

    def record(
        self,
        *,
        tool: str,
        ok: bool,
        error: str | None,
        duration_ms: float,
        result_bytes: int,
        details: dict[str, Any],
    ) -> None:
        outcome = outcome_of(ok, error, details)
        with self._lock:
            self._executions[(tool, outcome)] += 1
            self._duration_sum[tool] += duration_ms / 1000
            self._bytes[(tool, "result")] += result_bytes
            for kind in ("fetched_bytes", "file_bytes", "code_bytes"):
                value = details.get(kind)
                if isinstance(value, int) and value:
                    self._bytes[(tool, kind.removesuffix("_bytes"))] += value

    def lines(self) -> list[str]:
        with self._lock:
            executions = dict(self._executions)
            durations = dict(self._duration_sum)
            sizes = dict(self._bytes)
        out = [
            "# HELP fabric_dp_hosted_tool_executions_total Hosted tool calls, by outcome.",
            "# TYPE fabric_dp_hosted_tool_executions_total counter",
        ]
        for (tool, outcome), value in sorted(executions.items()):
            out.append(
                f'fabric_dp_hosted_tool_executions_total{{tool="{tool}",outcome="{outcome}"}} '
                f"{value}"
            )
        out += [
            "# HELP fabric_dp_hosted_tool_duration_seconds_total Time spent in hosted tools.",
            "# TYPE fabric_dp_hosted_tool_duration_seconds_total counter",
        ]
        for tool, value in sorted(durations.items()):
            out.append(f'fabric_dp_hosted_tool_duration_seconds_total{{tool="{tool}"}} {value:.3f}')
        out += [
            "# HELP fabric_dp_hosted_tool_bytes_total Bytes handled by hosted tools, by kind.",
            "# TYPE fabric_dp_hosted_tool_bytes_total counter",
        ]
        for (tool, kind), value in sorted(sizes.items()):
            out.append(f'fabric_dp_hosted_tool_bytes_total{{tool="{tool}",kind="{kind}"}} {value}')
        return out
