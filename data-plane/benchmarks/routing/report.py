"""Summaries, artifacts and figures for routing benchmark runs.

An artifact records everything needed to reproduce or question a result: the code commit
and whether the tree was dirty, the host model, the workload and its seed, the gateway's
measured overhead, and every request's outcome. It is labelled with what it can and cannot
support, because a simulated fleet compares routing decisions; it does not measure a GPU.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import json
import pathlib
import platform
import subprocess
import sys
from typing import Any

from benchmarks.routing.harness import RequestResult, RunResult

SCHEMA = "fabric.routing-benchmark/v1"

CLAIM_SCOPE = (
    "Routing strategies compared through the production gateway against simulated model "
    "hosts with a prefix cache and batch-dependent decode cost. Valid for comparing "
    "strategies with each other under this host model; not a measurement of any GPU, "
    "engine or deployment."
)


@dataclasses.dataclass(frozen=True)
class Slo:
    ttft_ms: float = 500.0
    tpot_ms: float = 60.0


def percentile(values: list[float], fraction: float) -> float | None:
    """Linear interpolation between closest ranks, as ``numpy.percentile`` does by default."""
    if not values:
        return None
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _ms(value: float | None) -> float | None:
    return None if value is None else round(value * 1000, 3)


def tpot(result: RequestResult) -> float | None:
    """Seconds per output token after the first, the decode speed a reader experiences."""
    if not result.ok or result.latency is None or result.output_tokens < 2:
        return None
    return (result.latency - result.ttft) / (result.output_tokens - 1)


def summarize(run: RunResult, slo: Slo) -> dict[str, Any]:
    requests = run.requests
    ok = [r for r in requests if r.ok]
    ttfts = [r.ttft for r in ok if r.ttft is not None]
    tpots = [value for r in ok if (value := tpot(r)) is not None]
    latencies = [r.latency for r in ok if r.latency is not None]
    prompt = sum(r.prompt_tokens for r in ok)
    cached = sum(r.cached_tokens for r in ok)
    span = run.span_seconds or 1.0
    per_backend: dict[str, int] = {}
    for r in ok:
        if r.backend:
            per_backend[r.backend] = per_backend.get(r.backend, 0) + 1
    counts = [per_backend.get(backend, 0) for backend in run.host_stats]
    mean = sum(counts) / len(counts) if counts else 0.0
    good = [
        r for r in ok
        if r.ttft * 1000 <= slo.ttft_ms
        and ((value := tpot(r)) is None or value * 1000 <= slo.tpot_ms)
    ]
    return {
        "requests": len(requests),
        "completed": len(ok),
        "failed": len(requests) - len(ok),
        "throughput_rps": round(len(ok) / span, 4),
        "output_tokens_per_second": round(sum(r.output_tokens for r in ok) / span, 2),
        "ttft_ms": {
            "p50": _ms(percentile(ttfts, 0.50)),
            "p95": _ms(percentile(ttfts, 0.95)),
            "p99": _ms(percentile(ttfts, 0.99)),
        },
        "tpot_ms": {"p50": _ms(percentile(tpots, 0.50)), "p95": _ms(percentile(tpots, 0.95))},
        "latency_ms": {
            "p50": _ms(percentile(latencies, 0.50)),
            "p95": _ms(percentile(latencies, 0.95)),
        },
        "cache_hit_rate": round(cached / prompt, 4) if prompt else 0.0,
        "prefill_tokens_computed": prompt - cached,
        "backend_requests": per_backend,
        # Busiest backend over the mean: 1.0 is perfectly even.
        "load_imbalance": round(max(counts) / mean, 4) if mean else None,
        "goodput_fraction": round(len(good) / len(requests), 4) if requests else 0.0,
        "goodput_rps": round(len(good) / span, 4),
        "span_seconds": round(run.span_seconds, 3),
        "wall_seconds": round(run.wall_seconds, 3),
    }


def _git() -> dict[str, Any]:
    def capture(*args: str) -> str | None:
        try:
            return subprocess.run(
                ["git", *args], capture_output=True, text=True, check=True
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    status = capture("status", "--porcelain", "--", ".")
    return {
        "commit": capture("rev-parse", "HEAD"),
        "dirty": bool(status) if status is not None else None,
    }


def build_artifact(
    runs: list[RunResult], *, slo: Slo, calibration: dict[str, float], argv: list[str]
) -> dict[str, Any]:
    first = runs[0].config if runs else None
    return {
        "schema": SCHEMA,
        "created_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "claim_scope": CLAIM_SCOPE,
        "citable_as_hardware_measurement": False,
        "command": argv,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "git": _git(),
        },
        "host_model": dataclasses.asdict(first.host) if first else None,
        "backends": first.backends if first else None,
        "time_scale": first.time_scale if first else None,
        "slo": dataclasses.asdict(slo),
        "calibration": calibration,
        "runs": [
            {
                "strategy": run.config.strategy,
                "workload": dataclasses.asdict(run.config.workload),
                "summary": summarize(run, slo),
                "host_stats": run.host_stats,
            }
            for run in runs
        ],
    }


def write_requests_csv(path: pathlib.Path, runs: list[RunResult]) -> None:
    fields = [field.name for field in dataclasses.fields(RequestResult)]
    with path.open("w", newline="") as target:
        writer = csv.writer(target)
        writer.writerow(["strategy", "workload", "rate", "seed", *fields])
        for run in runs:
            workload = run.config.workload
            for request in run.requests:
                row = dataclasses.asdict(request)
                writer.writerow([
                    run.config.strategy, workload.kind, workload.rate, workload.seed,
                    *(row[name] for name in fields),
                ])


def _aggregate(artifact: dict[str, Any]) -> dict[tuple[str, str, float], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str, float], list[dict[str, Any]]] = {}
    for run in artifact["runs"]:
        key = (run["workload"]["kind"], run["strategy"], run["workload"]["rate"])
        grouped.setdefault(key, []).append(run["summary"])
    return grouped


def _mean(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def _fmt(value: float | None, digits: int = 0) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def summary_markdown(artifact: dict[str, Any]) -> str:
    lines = [
        "# Routing benchmark",
        "",
        f"> {artifact['claim_scope']}",
        "",
        f"Commit `{(artifact['environment']['git']['commit'] or 'unknown')[:12]}`"
        f"{' (dirty tree)' if artifact['environment']['git']['dirty'] else ''}, "
        f"{artifact['backends']} backends, time scale {artifact['time_scale']}. "
        f"SLO: TTFT ≤ {artifact['slo']['ttft_ms']:.0f} ms and TPOT ≤ "
        f"{artifact['slo']['tpot_ms']:.0f} ms.",
        "",
    ]
    calibration = artifact["calibration"]
    if calibration.get("gateway_ttft_simulated_ms_p50") is not None:
        lines += [
            f"Gateway overhead (not simulated, included in every TTFT below): "
            f"{calibration['gateway_ttft_wall_ms_p50']:.1f} ms wall at p50, "
            f"≈ {calibration['gateway_ttft_simulated_ms_p50']:.0f} ms on the simulated clock.",
            "",
        ]
    grouped = _aggregate(artifact)
    for workload in sorted({key[0] for key in grouped}):
        lines += [
            f"## {workload}",
            "",
            "| Strategy | Rate (sessions/s) | Seeds | TTFT p50 ms | TTFT p95 ms | TPOT p95 ms "
            "| Cache hit | Load imbalance | Goodput | Failed |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for key in sorted(k for k in grouped if k[0] == workload):
            rows = grouped[key]
            lines.append(
                f"| {key[1]} | {key[2]:g} | {len(rows)} "
                f"| {_fmt(_mean([r['ttft_ms']['p50'] for r in rows]))} "
                f"| {_fmt(_mean([r['ttft_ms']['p95'] for r in rows]))} "
                f"| {_fmt(_mean([r['tpot_ms']['p95'] for r in rows]), 1)} "
                f"| {_fmt(_mean([r['cache_hit_rate'] * 100 for r in rows]), 1)}% "
                f"| {_fmt(_mean([r['load_imbalance'] for r in rows]), 2)} "
                f"| {_fmt(_mean([r['goodput_fraction'] * 100 for r in rows]), 1)}% "
                f"| {sum(r['failed'] for r in rows)} |"
            )
        lines.append("")
    return "\n".join(lines)


def write_figures(directory: pathlib.Path, artifact: dict[str, Any]) -> list[pathlib.Path]:
    """One figure per workload and metric, against offered load. Needs matplotlib."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    grouped = _aggregate(artifact)
    metrics = [
        ("ttft_p95", "TTFT p95 (ms)", lambda r: r["ttft_ms"]["p95"]),
        ("cache_hit", "Prompt tokens served from cache (%)", lambda r: r["cache_hit_rate"] * 100),
        ("goodput", "Requests meeting the SLO (%)", lambda r: r["goodput_fraction"] * 100),
    ]
    written: list[pathlib.Path] = []
    directory.mkdir(parents=True, exist_ok=True)
    for workload in sorted({key[0] for key in grouped}):
        for slug, label, read in metrics:
            figure, axis = plt.subplots(figsize=(6, 4))
            for strategy in sorted({key[1] for key in grouped if key[0] == workload}):
                keys = sorted((k for k in grouped if k[0] == workload and k[1] == strategy),
                              key=lambda k: k[2])
                rates = [k[2] for k in keys]
                values = [[read(r) for r in grouped[k] if read(r) is not None] for k in keys]
                means = [sum(v) / len(v) if v else float("nan") for v in values]
                low = [m - min(v) if v else 0 for m, v in zip(means, values, strict=True)]
                high = [max(v) - m if v else 0 for m, v in zip(means, values, strict=True)]
                axis.errorbar(rates, means, yerr=[low, high], marker="o", capsize=3,
                              label=strategy)
            axis.set_xlabel("Offered load (sessions per second)")
            axis.set_ylabel(label)
            axis.set_title(f"{workload}: {label}")
            axis.grid(alpha=0.3)
            axis.legend()
            figure.tight_layout()
            path = directory / f"{workload}-{slug}.svg"
            figure.savefig(path)
            plt.close(figure)
            written.append(path)
    return written


def write_all(
    directory: pathlib.Path,
    runs: list[RunResult],
    *,
    slo: Slo,
    calibration: dict[str, float],
    argv: list[str],
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    artifact = build_artifact(runs, slo=slo, calibration=calibration, argv=argv)
    (directory / "results.json").write_text(json.dumps(artifact, indent=2) + "\n")
    write_requests_csv(directory / "requests.csv", runs)
    (directory / "summary.md").write_text(summary_markdown(artifact))
    figures = write_figures(directory / "figures", artifact)
    artifact["figures"] = [str(path.relative_to(directory)) for path in figures]
    return artifact
