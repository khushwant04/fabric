"""One command per target: run the suite and emit a versioned artifact.

    python -m harness.runner --target rtx4070-dev

The measurement logic lives in ``benchmarks.gated_delta_decode_bench``; this
module only fixes the inputs, records the environment, and writes the artifact.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
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

        if not args.check_only:
            for batch in args.batches:
                measurements.append(
                    benchmark(
                        batch, args.heads, args.key_dim, args.value_dim, dtype, args.block_v
                    )
                )
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
    return 0 if artifact["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
