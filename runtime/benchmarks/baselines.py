"""Optimized third-party baselines.

Comparing Fabric kernels only against the local eager reference flatters them:
the reference is a readable formulation, not a tuned implementation. The
benchmark plan therefore requires a pinned optimized library baseline, which for
the gated-delta recurrence is flash-linear-attention's fused recurrent path.

A baseline is only citable once it is shown to compute the same thing, so every
timing here is paired with an equivalence check against the authoritative eager
reference. The baseline is optional: when the package is absent the suite still
runs and the artifact records its absence.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any

import torch

RUNTIME_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from kernels.gated_delta_decode import (  # noqa: E402
    gated_delta_decode,
    torch_gated_delta_decode_reference,
)

FLA_BASELINE = "flash-linear-attention:fused_recurrent_gated_delta_rule"


def fla_version() -> str | None:
    """Return the installed flash-linear-attention version, or ``None``."""
    try:
        import fla
    except ImportError:
        return None
    return getattr(fla, "__version__", "unknown")


def is_available() -> bool:
    return fla_version() is not None


def _fused_recurrent() -> Any:
    from fla.ops.gated_delta_rule import fused_recurrent_gated_delta_rule

    return fused_recurrent_gated_delta_rule


def fla_gated_delta_decode(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    g: torch.Tensor,
    beta: torch.Tensor,
    state: torch.Tensor,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Run one decode step through the FLA fused recurrent gated-delta path.

    Fabric passes ``[batch, heads, dim]`` for a single token while FLA expects a
    time axis, so inputs are unsqueezed to ``[batch, 1, heads, dim]``. ``g`` is
    the log decay in both formulations, and ``use_qk_l2norm_in_kernel`` matches
    Fabric's in-kernel normalization.

    Unlike the Fabric kernel, FLA returns a new state tensor rather than updating
    the state in place, so its timing includes that allocation.
    """
    key_dim = q.shape[-1]
    out, final_state = _fused_recurrent()(
        q.unsqueeze(1),
        k.unsqueeze(1),
        v.unsqueeze(1),
        g=g.unsqueeze(1),
        beta=beta.unsqueeze(1),
        scale=key_dim**-0.5,
        initial_state=state,
        output_final_state=True,
        use_qk_l2norm_in_kernel=True,
    )
    return out.squeeze(1), final_state


def equivalence(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    g: torch.Tensor,
    beta: torch.Tensor,
    state: torch.Tensor,
    atol: float,
) -> dict[str, Any]:
    """Compare the baseline against the authoritative eager reference.

    Also reports agreement with the Fabric kernel, which is what makes a latency
    comparison between the two meaningful.
    """
    reference_state = state.clone()
    reference_out = torch_gated_delta_decode_reference(q, k, v, g, beta, reference_state)

    fabric_state = state.clone()
    fabric_out = gated_delta_decode(q, k, v, g, beta, fabric_state)

    baseline_out, baseline_state = fla_gated_delta_decode(q, k, v, g, beta, state.clone())
    torch.cuda.synchronize()

    output_error = (baseline_out.float() - reference_out.float()).abs().max().item()
    state_error = (baseline_state.float() - reference_state).abs().max().item()
    return {
        "output_max_abs": output_error,
        "state_max_abs": state_error,
        "output_max_abs_vs_fabric": (
            (baseline_out.float() - fabric_out.float()).abs().max().item()
        ),
        "state_max_abs_vs_fabric": (
            (baseline_state.float() - fabric_state).abs().max().item()
        ),
        "within_atol": output_error <= atol,
        "atol": atol,
    }


def benchmark_against_fabric(
    batch: int,
    heads: int,
    key_dim: int,
    value_dim: int,
    dtype: torch.dtype,
    block_v: int,
    warmup: int = 100,
    reps: int = 500,
) -> dict[str, Any]:
    """Time the baseline and the Fabric kernel on identical inputs."""
    import triton

    from benchmarks.gated_delta_decode_bench import make_inputs

    q, k, v, g, beta, initial_state = make_inputs(
        batch, heads, key_dim, value_dim, dtype, seed=200 + batch
    )
    baseline_state = initial_state.clone()
    fabric_state = initial_state.clone()
    fabric_out = torch.empty_like(v)

    fla_gated_delta_decode(q, k, v, g, beta, baseline_state)
    gated_delta_decode(q, k, v, g, beta, fabric_state, out=fabric_out, block_v=block_v)
    torch.cuda.synchronize()

    baseline_ms = triton.testing.do_bench(
        lambda: fla_gated_delta_decode(q, k, v, g, beta, baseline_state),
        warmup=warmup,
        rep=reps,
    )
    fabric_ms = triton.testing.do_bench(
        lambda: gated_delta_decode(
            q, k, v, g, beta, fabric_state, out=fabric_out, block_v=block_v
        ),
        warmup=warmup,
        rep=reps,
    )
    return {
        "batch": batch,
        "baseline_ms": baseline_ms,
        "fabric_ms": fabric_ms,
        # Above 1.0 means the Fabric kernel is faster than the baseline.
        "fabric_speedup_vs_baseline": baseline_ms / fabric_ms,
    }
