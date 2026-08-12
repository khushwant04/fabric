"""One command per target: run the suite and emit a versioned artifact.

    python -m harness.runner --target rtx4070-dev

The measurement logic lives in ``benchmarks.gated_delta_decode_bench``; this
module only fixes the inputs, records the environment, and writes the artifact.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from typing import Any

RUNTIME_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from harness import environment, kernel_metadata  # noqa: E402
from harness.artifact import (  # noqa: E402
    TARGETS,
    Target,
    build_artifact,
    resolve_target,
    verify_target,
    write_artifact,
)

SUITE = "gated-delta-decode-microbench"
DEFAULT_ARTIFACT_ROOT = RUNTIME_ROOT / "artifacts"

#: Seeds used by the correctness sweep, recorded so a run can be reproduced.
CORRECTNESS_SEEDS = (0, 1, 17)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        required=True,
        choices=sorted(TARGETS),
        help="Hardware role this run belongs to; must match the GPU present",
    )
    parser.add_argument("--dtype", choices=("float16", "bfloat16", "float32"), default="float16")
    parser.add_argument("--block-v", type=int, choices=(8, 16, 32), default=32)
    parser.add_argument("--batches", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--heads", type=int, default=8)
    parser.add_argument("--key-dim", type=int, default=64)
    parser.add_argument("--value-dim", type=int, default=64)
    parser.add_argument(
        "--check-only", action="store_true", help="Run correctness without timing"
    )
    parser.add_argument(
        "--drift-steps",
        type=int,
        default=1024,
        help="Decode steps for the long-generation drift check (0 disables it)",
    )
    parser.add_argument(
        "--sweep-block-v",
        type=int,
        nargs="*",
        default=[8, 16, 32],
        help="Value tiles to sweep with compiled-kernel metadata (empty disables)",
    )
    parser.add_argument(
        "--no-baselines",
        dest="baselines",
        action="store_false",
        help="Skip the optimized-library baseline comparison",
    )
    parser.add_argument(
        "--no-profile",
        dest="profile",
        action="store_false",
        help="Skip occupancy, bandwidth utilization, and compile-latency profiling",
    )
    parser.add_argument(
        "--no-compile-probe",
        dest="compile_probe",
        action="store_false",
        help="Skip the cold/warm first-use latency subprocesses",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the artifact instead of writing it"
    )
    parser.add_argument("--artifact-root", type=pathlib.Path, default=DEFAULT_ARTIFACT_ROOT)
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> dict[str, Any]:
    """Return the immutable configuration recorded on the artifact."""
    return {
        "dtype": args.dtype,
        "state_dtype": "float32",
        "block_v": args.block_v,
        "batches": list(args.batches),
        "heads": args.heads,
        "key_dim": args.key_dim,
        "value_dim": args.value_dim,
        "correctness_seeds": list(CORRECTNESS_SEEDS),
        "benchmark_warmup": 100,
        "benchmark_reps": 500,
        "drift_steps": args.drift_steps,
        "drift_batch": args.batches[0],
        "sweep_block_v": list(args.sweep_block_v),
        "baselines_requested": bool(args.baselines),
        "profile_requested": bool(args.profile),
        "compile_probe_requested": bool(args.compile_probe),
        "shapes_are_synthetic": True,
    }


def sweep_value_tiles(args: argparse.Namespace, dtype: Any) -> list[dict[str, Any]]:
    """Measure every value tile across every batch, with what each tile compiled to.

    Runs before any other launch so each tile is compiled for the first time
    here and its metadata can be attributed to this configuration. Timings cover
    all requested batches because the best tile depends on how much work is
    available to fill the GPU, so a single batch would not justify a default.
    """
    from benchmarks.gated_delta_decode_bench import benchmark, make_inputs
    from kernels.gated_delta_decode import _gated_delta_decode_kernel, gated_delta_decode

    results: list[dict[str, Any]] = []
    for block_v in args.sweep_block_v:
        inputs = make_inputs(
            args.batches[0], args.heads, args.key_dim, args.value_dim, dtype, seed=7
        )
        metadata = kernel_metadata.capture(
            _gated_delta_decode_kernel,
            lambda inputs=inputs, block_v=block_v: gated_delta_decode(
                *inputs, block_v=block_v
            ),
        )
        timings = [
            {
                "batch": batch,
                "triton_ms": timing["triton_ms"],
                "state_bandwidth_gbps": timing["state_bandwidth_gbps"],
            }
            for batch in args.batches
            for timing in [
                benchmark(batch, args.heads, args.key_dim, args.value_dim, dtype, block_v)
            ]
        ]
        results.append({"block_v": block_v, "kernel": metadata, "timings": timings})
    return results


def fastest_tile_per_batch(tile_sweep: list[dict[str, Any]]) -> dict[str, int]:
    """Return the lowest-latency value tile for each batch in the sweep."""
    best: dict[str, tuple[float, int]] = {}
    for entry in tile_sweep:
        for timing in entry["timings"]:
            batch = str(timing["batch"])
            candidate = (timing["triton_ms"], entry["block_v"])
            if batch not in best or candidate < best[batch]:
                best[batch] = candidate
    return {batch: block_v for batch, (_ms, block_v) in best.items()}


def run_baselines(args: argparse.Namespace, dtype: Any) -> list[dict[str, Any]] | None:
    """Compare the Fabric kernel against pinned optimized libraries.

    Returns ``None`` when no baseline library is installed, which is recorded on
    the artifact so a reader can tell "not compared" from "compared and equal".
    """
    from benchmarks import baselines
    from benchmarks.gated_delta_decode_bench import make_inputs, tolerances

    if not baselines.is_available():
        return None

    atol, _rtol = tolerances(dtype)
    inputs = make_inputs(args.batches[0], args.heads, args.key_dim, args.value_dim, dtype, seed=3)
    entry = {
        "name": baselines.FLA_BASELINE,
        "version": baselines.fla_version(),
        "equivalence": baselines.equivalence(*inputs, atol=atol),
        "timings": [
            baselines.benchmark_against_fabric(
                batch, args.heads, args.key_dim, args.value_dim, dtype, args.block_v
            )
            for batch in args.batches
        ],
    }
    return [entry]


def _compile_probe(args: argparse.Namespace, *, cache_dir: str | None) -> dict[str, Any] | None:
    """Run the first-use probe in a fresh process, optionally with a cold cache."""
    command = [
        sys.executable,
        "-m",
        "harness.compile_probe",
        "--dtype",
        args.dtype,
        "--block-v",
        str(args.block_v),
        "--heads",
        str(args.heads),
        "--key-dim",
        str(args.key_dim),
        "--value-dim",
        str(args.value_dim),
    ]
    child_env = dict(os.environ)
    if cache_dir is not None:
        child_env["TRITON_CACHE_DIR"] = cache_dir
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argument vector, no shell
            command,
            cwd=RUNTIME_ROOT,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)[:500]}
    if completed.returncode != 0:
        return {"error": (completed.stderr or "probe failed").strip()[:500]}
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "probe produced no parsable result"}


def compile_latency(args: argparse.Namespace) -> dict[str, Any]:
    """Measure first-use latency with a cold Triton cache and with the normal one."""
    with tempfile.TemporaryDirectory(prefix="fabric-triton-cache-") as cold_cache:
        cold = _compile_probe(args, cache_dir=cold_cache)
    warm = _compile_probe(args, cache_dir=None)
    return {
        "cold_cache": cold,
        "warm_cache": warm,
        "note": (
            "cold_cache uses an isolated TRITON_CACHE_DIR so it includes "
            "compilation; warm_cache reuses the on-disk cache."
        ),
    }


def run_profile(
    args: argparse.Namespace,
    tile_sweep: list[dict[str, Any]] | None,
    measurements: list[dict[str, Any]],
    dtype: Any,
) -> dict[str, Any]:
    """Collect the profiling metrics obtainable without hardware counters."""
    import torch

    from harness import profiling

    limits = profiling.device_limits(torch.cuda.get_device_properties(0))
    element_size = torch.empty((), dtype=dtype).element_size()

    occupancy = []
    for entry in tile_sweep or []:
        kernel = entry.get("kernel") or {}
        occupancy.append(
            {
                "block_v": entry["block_v"],
                "theoretical": profiling.theoretical_occupancy(
                    registers=kernel.get("registers"),
                    shared_memory_bytes=kernel.get("shared_memory_bytes"),
                    num_warps=kernel.get("num_warps"),
                    limits=limits,
                ),
            }
        )

    bandwidth: list[dict[str, Any]] = []
    if measurements:
        copy_bandwidth = profiling.measure_copy_bandwidth()
        for entry in measurements:
            moved = profiling.bytes_moved(
                batch=entry["batch"],
                heads=args.heads,
                key_dim=args.key_dim,
                value_dim=args.value_dim,
                element_size=element_size,
            )
            bandwidth.append(
                {
                    "batch": entry["batch"],
                    "bytes_per_launch": moved,
                    **profiling.utilization(
                        bytes_per_launch=moved,
                        launch_ms=entry["triton_ms"],
                        copy_bandwidth_gbps=copy_bandwidth,
                    ),
                }
            )

    return {
        "device_limits": limits,
        "occupancy": occupancy,
        "bandwidth": bandwidth,
        "compile_latency": compile_latency(args) if args.compile_probe else None,
        # Counter-based metrics are recorded as absent with the reason, not omitted.
        "counter_metrics": {
            metric: None for metric in profiling.COUNTER_ONLY_METRICS
        },
        "counter_metrics_unavailable_reason": profiling.COUNTER_METRICS_UNAVAILABLE,
    }


def run_suite(args: argparse.Namespace, target: Target) -> dict[str, Any]:
    """Execute the suite phases and assemble the artifact."""
    import torch

    from benchmarks.gated_delta_decode_bench import (
        benchmark,
        check_correctness,
        measure_drift,
    )

    captured = environment.capture()
    verify_target(target, captured)

    dtype = getattr(torch, args.dtype)
    correctness: list[dict[str, Any]] = []
    measurements: list[dict[str, Any]] = []
    tile_sweep: list[dict[str, Any]] | None = None
    drift: dict[str, Any] | None = None
    baselines: list[dict[str, Any]] | None = None
    profile: dict[str, Any] | None = None
    status = "pass"

    try:
        # Sweep first: metadata capture needs a cold compilation cache per tile.
        if args.sweep_block_v and not args.check_only:
            tile_sweep = sweep_value_tiles(args, dtype)

        for batch in args.batches:
            errors = check_correctness(
                batch, args.heads, args.key_dim, args.value_dim, dtype, args.block_v
            )
            correctness.append({"batch": batch, **errors})

        if args.drift_steps:
            drift = measure_drift(
                args.batches[0],
                args.heads,
                args.key_dim,
                args.value_dim,
                dtype,
                args.block_v,
                steps=args.drift_steps,
            )

        if args.baselines and not args.check_only:
            baselines = run_baselines(args, dtype)

        if not args.check_only:
            for batch in args.batches:
                measurements.append(
                    benchmark(
                        batch, args.heads, args.key_dim, args.value_dim, dtype, args.block_v
                    )
                )

        if args.profile and not args.check_only:
            profile = run_profile(args, tile_sweep, measurements, dtype)
    except AssertionError as exc:
        status = "fail"
        correctness.append({"error": str(exc)[:2000]})

    return build_artifact(
        target=target,
        suite=SUITE,
        status=status,
        environment=captured,
        config=build_config(args),
        correctness=correctness,
        measurements=measurements,
        drift=drift,
        tile_sweep=tile_sweep,
        baselines=baselines,
        profile=profile,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    target = resolve_target(args.target)
    artifact = run_suite(args, target)

    if args.dry_run:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    else:
        path = write_artifact(args.artifact_root, artifact)
        print(f"artifact={path.relative_to(RUNTIME_ROOT.parent)}")

    gpu = artifact["environment"]["gpu"]
    print(
        f"target={artifact['target']} gpu={gpu['name']} status={artifact['status']} "
        f"hash={artifact['content_hash'][:12]}"
    )
    for entry in artifact["measurements"]:
        print(
            f"batch={entry['batch']:<3} eager_ms={entry['reference_ms']:.4f} "
            f"triton_ms={entry['triton_ms']:.4f} speedup={entry['speedup']:.2f}x "
            f"state_GB/s={entry['state_bandwidth_gbps']:.1f}"
        )

    for entry in artifact["tile_sweep"] or []:
        kernel = entry["kernel"] or {}
        latencies = " ".join(
            f"b{timing['batch']}={timing['triton_ms']:.4f}" for timing in entry["timings"]
        )
        print(
            f"block_v={entry['block_v']:<3} regs={kernel.get('registers')} "
            f"spills={kernel.get('spills')} shared={kernel.get('shared_memory_bytes')}B "
            f"warps={kernel.get('num_warps')} ms[{latencies}]"
        )
    if artifact["tile_sweep"]:
        fastest = fastest_tile_per_batch(artifact["tile_sweep"])
        print(
            "fastest block_v per batch: "
            + " ".join(f"b{batch}={block_v}" for batch, block_v in sorted(fastest.items()))
            + f" (kernel default is {artifact['config']['block_v']})"
        )

    drift = artifact["drift"]
    if drift:
        print(
            f"drift steps={drift['steps']} output_max_abs={drift['output_max_abs']:.6g} "
            f"state_max_abs={drift['state_max_abs']:.6g} "
            f"final_state_max_rel={drift['final_state_max_rel']:.3%} "
            f"within_single_step_atol={drift['within_single_step_atol']}"
        )

    for entry in artifact["baselines"] or []:
        equivalence = entry["equivalence"]
        print(
            f"baseline {entry['name']}=={entry['version']} "
            f"equivalent={equivalence['within_atol']} "
            f"out_vs_ref={equivalence['output_max_abs']:.3g} "
            f"out_vs_fabric={equivalence['output_max_abs_vs_fabric']:.3g}"
        )
        for timing in entry["timings"]:
            print(
                f"  batch={timing['batch']:<3} baseline_ms={timing['baseline_ms']:.4f} "
                f"fabric_ms={timing['fabric_ms']:.4f} "
                f"fabric_speedup={timing['fabric_speedup_vs_baseline']:.2f}x"
            )
    if artifact["config"]["baselines_requested"] and not artifact["baselines"]:
        print("baseline: no optimized library installed; comparison not run")

    profile = artifact["profile"]
    if profile:
        for entry in profile["occupancy"]:
            theoretical = entry["theoretical"] or {}
            print(
                f"occupancy block_v={entry['block_v']:<3} "
                f"theoretical={theoretical.get('occupancy', 0):.0%} "
                f"warps/SM={theoretical.get('active_warps_per_sm')}/"
                f"{theoretical.get('max_warps_per_sm')} "
                f"limited_by={theoretical.get('limited_by')}"
            )
        for entry in profile["bandwidth"]:
            print(
                f"bandwidth batch={entry['batch']:<3} "
                f"achieved={entry['achieved_gbps']:.1f}GB/s "
                f"of_copy_bw={entry['fraction_of_copy_bandwidth']:.0%}"
            )
        latency = profile["compile_latency"]
        if latency:
            cold = latency["cold_cache"] or {}
            warm = latency["warm_cache"] or {}
            print(
                f"first_use cold={cold.get('first_call_ms', float('nan')):.0f}ms "
                f"warm={warm.get('first_call_ms', float('nan')):.0f}ms "
                f"steady={warm.get('steady_state_ms', float('nan')):.4f}ms"
            )
        print(f"counter metrics: not collected ({profile['counter_metrics_unavailable_reason']})")
    return 0 if artifact["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
