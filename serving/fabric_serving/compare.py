"""Compare the Fabric substitution against vLLM's native gated-delta decode.

This is the plan's "pinned standard vLLM" baseline at the kernel level: identical
inputs, identical paged-cache semantics, one token per sequence.

    python -m fabric_serving.compare --sequences 1 2 4 8 16
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import torch
import triton
from vllm.model_executor.layers.fla.ops.fused_recurrent import (
    fused_recurrent_gated_delta_rule as vllm_fused,
)

from fabric_serving.vllm_gated_delta import fabric_fused_recurrent_gated_delta_rule


def make_decode_batch(
    sequences: int,
    heads: int,
    key_dim: int,
    value_dim: int,
    slots: int,
    dtype: torch.dtype,
    seed: int = 0,
) -> dict[str, Any]:
    """Build one decode step in vLLM's layout, with a shuffled slot table."""
    generator = torch.Generator(device="cuda").manual_seed(seed)
    shape = (1, sequences, heads)
    q = torch.randn(*shape, key_dim, dtype=dtype, device="cuda", generator=generator)
    k = torch.randn_like(q)
    v = torch.randn(*shape, value_dim, dtype=dtype, device="cuda", generator=generator)
    g = -torch.rand(*shape, dtype=torch.float32, device="cuda", generator=generator)
    beta = torch.rand(*shape, dtype=torch.float32, device="cuda", generator=generator)
    cache = (
        torch.randn(
            slots, heads, key_dim, value_dim, dtype=torch.float32,
            device="cuda", generator=generator,
        )
        * 0.05
    )
    # Non-identity mapping so a slot-indexing bug cannot pass unnoticed.
    indices = torch.randperm(slots, generator=generator, device="cuda")[:sequences]
    return {
        "q": q,
        "k": k,
        "v": v,
        "g": g,
        "beta": beta,
        "cache": cache,
        "indices": indices.to(torch.int32).contiguous(),
        "cu_seqlens": torch.arange(sequences + 1, dtype=torch.int32, device="cuda"),
        "scale": key_dim**-0.5,
    }


def _call_vllm(batch: dict[str, Any], cache: torch.Tensor) -> torch.Tensor:
    out, _state = vllm_fused(
        q=batch["q"],
        k=batch["k"],
        v=batch["v"],
        g=batch["g"],
        beta=batch["beta"],
        scale=batch["scale"],
        initial_state=cache,
        inplace_final_state=True,
        cu_seqlens=batch["cu_seqlens"],
        ssm_state_indices=batch["indices"],
        use_qk_l2norm_in_kernel=True,
    )
    return out


def _call_fabric(batch: dict[str, Any], cache: torch.Tensor) -> torch.Tensor:
    out, _state = fabric_fused_recurrent_gated_delta_rule(
        q=batch["q"],
        k=batch["k"],
        v=batch["v"],
        g=batch["g"],
        beta=batch["beta"],
        scale=batch["scale"],
        initial_state=cache,
        inplace_final_state=True,
        cu_seqlens=batch["cu_seqlens"],
        ssm_state_indices=batch["indices"],
        use_qk_l2norm_in_kernel=True,
        mode="fabric",
    )
    return out


def compare(batch: dict[str, Any], warmup: int = 50, reps: int = 200) -> dict[str, Any]:
    """Check equivalence on identical inputs, then time both paths."""
    vllm_cache = batch["cache"].clone()
    fabric_cache = batch["cache"].clone()

    vllm_out = _call_vllm(batch, vllm_cache)
    fabric_out = _call_fabric(batch, fabric_cache)
    torch.cuda.synchronize()

    output_delta = (fabric_out.float() - vllm_out.float()).abs().max().item()
    cache_delta = (fabric_cache - vllm_cache).abs().max().item()
    untouched = batch["cache"].shape[0] > batch["q"].shape[1]

    vllm_ms = triton.testing.do_bench(
        lambda: _call_vllm(batch, vllm_cache), warmup=warmup, rep=reps
    )
    fabric_ms = triton.testing.do_bench(
        lambda: _call_fabric(batch, fabric_cache), warmup=warmup, rep=reps
    )
    return {
        "sequences": int(batch["q"].shape[1]),
        "output_max_abs_vs_vllm": output_delta,
        "cache_max_abs_vs_vllm": cache_delta,
        "slot_table_is_shuffled": bool(untouched),
        "vllm_ms": vllm_ms,
        "fabric_ms": fabric_ms,
        "fabric_speedup_vs_vllm": vllm_ms / fabric_ms,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sequences", type=int, nargs="+", default=[1, 2, 4, 8, 16])
    parser.add_argument("--heads", type=int, default=8)
    parser.add_argument("--key-dim", type=int, default=64)
    parser.add_argument("--value-dim", type=int, default=64)
    parser.add_argument("--slots", type=int, default=64)
    parser.add_argument("--dtype", choices=("float16", "bfloat16"), default="float16")
    parser.add_argument("--json", action="store_true", help="Emit results as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not torch.cuda.is_available():
        raise SystemExit("CUDA is required")

    dtype = getattr(torch, args.dtype)
    results = [
        compare(
            make_decode_batch(
                sequences, args.heads, args.key_dim, args.value_dim, args.slots, dtype
            )
        )
        for sequences in args.sequences
    ]

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"{'seqs':>5} {'vllm_ms':>9} {'fabric_ms':>10} {'speedup':>8} {'out_delta':>10}")
    for row in results:
        print(
            f"{row['sequences']:>5} {row['vllm_ms']:>9.4f} {row['fabric_ms']:>10.4f} "
            f"{row['fabric_speedup_vs_vllm']:>7.2f}x {row['output_max_abs_vs_vllm']:>10.3g}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
