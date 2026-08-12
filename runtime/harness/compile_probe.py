"""First-use latency probe, run as its own process.

Compile latency cannot be measured honestly inside a process that has already
compiled the kernel, and Triton also caches compiled kernels on disk between
processes. This probe therefore runs standalone and reports the latency of the
first call, so the caller can invoke it twice: once with an isolated
``TRITON_CACHE_DIR`` for a cold compile, and once with the normal cache for a
warm start.

    python -m harness.compile_probe --block-v 32 --dtype float16
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

RUNTIME_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dtype", choices=("float16", "bfloat16", "float32"), default="float16")
    parser.add_argument("--block-v", type=int, choices=(8, 16, 32), default=32)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--heads", type=int, default=8)
    parser.add_argument("--key-dim", type=int, default=64)
    parser.add_argument("--value-dim", type=int, default=64)
    return parser.parse_args(argv)


def probe(args: argparse.Namespace) -> dict[str, float | int | str]:
    """Return first-call and steady-state launch latency in milliseconds."""
    import torch

    from benchmarks.gated_delta_decode_bench import make_inputs
    from kernels.gated_delta_decode import gated_delta_decode

    dtype = getattr(torch, args.dtype)
    q, k, v, g, beta, state = make_inputs(
        args.batch, args.heads, args.key_dim, args.value_dim, dtype, seed=11
    )
    out = torch.empty_like(v)

    started = time.perf_counter()
    gated_delta_decode(q, k, v, g, beta, state, out=out, block_v=args.block_v)
    torch.cuda.synchronize()
    first_call_ms = (time.perf_counter() - started) * 1e3

    # Steady state: the same launch once the kernel is resident.
    for _ in range(10):
        gated_delta_decode(q, k, v, g, beta, state, out=out, block_v=args.block_v)
    torch.cuda.synchronize()
    started = time.perf_counter()
    for _ in range(100):
        gated_delta_decode(q, k, v, g, beta, state, out=out, block_v=args.block_v)
    torch.cuda.synchronize()
    steady_ms = (time.perf_counter() - started) * 1e3 / 100

    return {
        "block_v": args.block_v,
        "dtype": args.dtype,
        "first_call_ms": first_call_ms,
        "steady_state_ms": steady_ms,
        # The directory actually in effect, which the caller may have isolated.
        "triton_cache_dir": os.environ.get(
            "TRITON_CACHE_DIR", str(pathlib.Path.home() / ".triton")
        ),
    }


def main(argv: list[str] | None = None) -> int:
    print(json.dumps(probe(parse_args(argv))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
