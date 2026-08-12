"""Correctness and microbenchmark harness for the gated-delta decode kernel."""

from __future__ import annotations

import argparse
import pathlib
import sys

import torch
import triton


RUNTIME_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from kernels.gated_delta_decode import (  # noqa: E402
    gated_delta_decode,
    torch_gated_delta_decode_reference,
)


def make_inputs(
    batch: int,
    heads: int,
    key_dim: int,
    value_dim: int,
    dtype: torch.dtype,
    seed: int = 0,
):
    generator = torch.Generator(device="cuda")
    generator.manual_seed(seed)
    q = torch.randn(
        batch,
        heads,
        key_dim,
        dtype=dtype,
        device="cuda",
        generator=generator,
    )
    k = torch.randn_like(q)
    v = torch.randn(
        batch,
        heads,
        value_dim,
        dtype=dtype,
        device="cuda",
        generator=generator,
    )
    # The supported execution path computes g and beta around the recurrence in FP32.
    g = -torch.rand(
        batch,
        heads,
        dtype=torch.float32,
        device="cuda",
        generator=generator,
    )
    beta = torch.rand_like(g)
    state = torch.randn(
        batch,
        heads,
        key_dim,
        value_dim,
        dtype=torch.float32,
        device="cuda",
        generator=generator,
    ) * 0.05
    return q, k, v, g, beta, state


def tolerances(dtype: torch.dtype) -> tuple[float, float]:
    if dtype == torch.float32:
        return 2e-5, 2e-5
    if dtype == torch.float16:
        return 2e-3, 2e-3
    return 2e-2, 2e-2


def check_correctness(
    batch: int,
    heads: int,
    key_dim: int,
    value_dim: int,
    dtype: torch.dtype,
    block_v: int,
) -> dict[str, float]:
    max_output_error = 0.0
    max_state_error = 0.0
    atol, rtol = tolerances(dtype)

    for seed in (0, 1, 17):
        q, k, v, g, beta, initial_state = make_inputs(
            batch, heads, key_dim, value_dim, dtype, seed=seed
        )
        reference_state = initial_state.clone()
        triton_state = initial_state.clone()

        reference_out = torch_gated_delta_decode_reference(q, k, v, g, beta, reference_state)
        triton_out = gated_delta_decode(q, k, v, g, beta, triton_state, block_v=block_v)
        torch.cuda.synchronize()

        torch.testing.assert_close(triton_out, reference_out, atol=atol, rtol=rtol)
        torch.testing.assert_close(triton_state, reference_state, atol=atol, rtol=rtol)
        max_output_error = max(
            max_output_error,
            (triton_out.float() - reference_out.float()).abs().max().item(),
        )
        max_state_error = max(max_state_error, (triton_state - reference_state).abs().max().item())

    return {
        "output_max_abs": max_output_error,
        "state_max_abs": max_state_error,
    }


def benchmark(
    batch: int,
    heads: int,
    key_dim: int,
    value_dim: int,
    dtype: torch.dtype,
    block_v: int,
) -> dict[str, float]:
    q, k, v, g, beta, initial_state = make_inputs(
        batch, heads, key_dim, value_dim, dtype, seed=100 + batch
    )
    reference_state = initial_state.clone()
    triton_state = initial_state.clone()
    reference_out = torch.empty_like(v)
    triton_out = torch.empty_like(v)

    # Compile and warm both paths before timing.
    torch_gated_delta_decode_reference(q, k, v, g, beta, reference_state, out=reference_out)
    gated_delta_decode(q, k, v, g, beta, triton_state, out=triton_out, block_v=block_v)
    torch.cuda.synchronize()

    reference_ms = triton.testing.do_bench(
        lambda: torch_gated_delta_decode_reference(
            q, k, v, g, beta, reference_state, out=reference_out
        ),
        warmup=100,
        rep=500,
    )
    triton_ms = triton.testing.do_bench(
        lambda: gated_delta_decode(
            q, k, v, g, beta, triton_state, out=triton_out, block_v=block_v
        ),
        warmup=100,
        rep=500,
    )

    state_bytes = triton_state.numel() * triton_state.element_size() * 2
    return {
        "batch": batch,
        "reference_ms": reference_ms,
        "triton_ms": triton_ms,
        "speedup": reference_ms / triton_ms,
        "state_bandwidth_gbps": state_bytes / (triton_ms * 1e-3) / 1e9,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--dtype", choices=("float16", "bfloat16", "float32"), default="float16")
    parser.add_argument("--block-v", type=int, choices=(8, 16, 32), default=32)
    parser.add_argument("--batches", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--heads", type=int, default=8)
    parser.add_argument("--key-dim", type=int, default=64)
    parser.add_argument("--value-dim", type=int, default=64)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not torch.cuda.is_available():
        raise SystemExit("CUDA is required")

    dtype = getattr(torch, args.dtype)
    props = torch.cuda.get_device_properties(0)
    print(
        f"device={props.name} cc={props.major}.{props.minor} "
        f"torch={torch.__version__} cuda={torch.version.cuda} triton={triton.__version__}"
    )
    print(
        f"shape=H{args.heads}xK{args.key_dim}xV{args.value_dim} "
        f"dtype={args.dtype} state=float32 block_v={args.block_v}"
    )

    for batch in args.batches:
        errors = check_correctness(
            batch,
            args.heads,
            args.key_dim,
            args.value_dim,
            dtype,
            args.block_v,
        )
        print(
            f"correct batch={batch:<2} output_max_abs={errors['output_max_abs']:.6g} "
            f"state_max_abs={errors['state_max_abs']:.6g}"
        )

    if args.check_only:
        return

    print("batch  eager_ms  triton_ms  speedup  state_GB/s")
    for batch in args.batches:
        result = benchmark(
            batch,
            args.heads,
            args.key_dim,
            args.value_dim,
            dtype,
            args.block_v,
        )
        print(
            f"{batch:>5}  {result['reference_ms']:>8.4f}  {result['triton_ms']:>9.4f}  "
            f"{result['speedup']:>7.2f}x  {result['state_bandwidth_gbps']:>10.1f}"
        )


if __name__ == "__main__":
    main()
