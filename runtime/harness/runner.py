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

from harness import environment  # noqa: E402
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
        "shapes_are_synthetic": True,
    }


def run_suite(args: argparse.Namespace, target: Target) -> dict[str, Any]:
    """Execute correctness and, unless suppressed, timing for every batch."""
    import torch

    from benchmarks.gated_delta_decode_bench import benchmark, check_correctness

    captured = environment.capture()
    verify_target(target, captured)

    dtype = getattr(torch, args.dtype)
    correctness: list[dict[str, Any]] = []
    measurements: list[dict[str, Any]] = []
    status = "pass"

    try:
        for batch in args.batches:
            errors = check_correctness(
                batch, args.heads, args.key_dim, args.value_dim, dtype, args.block_v
            )
            correctness.append({"batch": batch, **errors})

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
    return 0 if artifact["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
