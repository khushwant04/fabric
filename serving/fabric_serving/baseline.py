"""The vLLM decode-op comparison, in the shape the measurement artifact records.

`compare.py` prints JSON for a human. That is not evidence: it carries no schema, no
environment capture, no content hash, and no hardware guard, so a number taken from it
cannot be tied to the machine that produced it. The project's documentation policy says
benchmark claims stay attached to reproducible artifacts, and the comparison against
vLLM's own kernel is the strongest claim Fabric makes, so it belongs in the artifact
chain rather than in a terminal scrollback.

This module lives in `serving/` because vLLM does. The harness discovers it optionally,
which keeps the dependency pointing one way: `serving` depends on `runtime`, and the
runtime harness works whether or not vLLM is installed, recording which was the case.
"""

from __future__ import annotations

from typing import Any

#: Identifies the compared implementation on the artifact. The operation is named
#: because vLLM contains several attention paths and only this one is substituted.
NAME = "vllm:fused_recurrent_gated_delta_rule"


def version() -> str | None:
    try:
        import vllm
    except ImportError:
        return None
    return getattr(vllm, "__version__", None)


def is_available() -> bool:
    """Whether the comparison can run in this environment."""
    try:
        import vllm  # noqa: F401
        from vllm.model_executor.layers.fla.ops import (  # noqa: F401
            fused_recurrent_gated_delta_rule,
        )
    except Exception:  # noqa: BLE001 - a partial install is unavailable, not fatal
        return False
    return True


def measure(
    *,
    sequences: list[int],
    heads: int,
    key_dim: int,
    value_dim: int,
    dtype: Any,
    slots: int = 64,
    warmup: int = 50,
    reps: int = 200,
) -> dict[str, Any]:
    """Compare against vLLM's kernel across sequence counts.

    Returns one entry shaped like the other baselines: an equivalence result taken
    once, then a timing per sequence count. Equivalence is reported as the maximum
    absolute difference in both the output and the state cache, because a decode
    kernel that matched outputs while corrupting cached state would be wrong in a way
    a single-step output check cannot see.
    """
    from fabric_serving.compare import compare, make_decode_batch

    timings: list[dict[str, Any]] = []
    equivalence: dict[str, Any] | None = None

    for count in sequences:
        batch = make_decode_batch(
            sequences=count,
            heads=heads,
            key_dim=key_dim,
            value_dim=value_dim,
            slots=slots,
            dtype=dtype,
        )
        result = compare(batch, warmup=warmup, reps=reps)

        if equivalence is None:
            equivalence = {
                "output_max_abs_vs_vllm": result["output_max_abs_vs_vllm"],
                "cache_max_abs_vs_vllm": result["cache_max_abs_vs_vllm"],
                # Bit-identical is a stronger statement than within-tolerance, and it
                # is what this substitution actually achieves, so it is recorded as
                # its own fact rather than folded into a tolerance check.
                "bit_identical": (
                    result["output_max_abs_vs_vllm"] == 0.0
                    and result["cache_max_abs_vs_vllm"] == 0.0
                ),
                "slot_table_is_shuffled": result["slot_table_is_shuffled"],
                "sequences": result["sequences"],
            }

        timings.append(
            {
                "sequences": result["sequences"],
                "baseline_ms": result["vllm_ms"],
                "fabric_ms": result["fabric_ms"],
                "fabric_speedup_vs_baseline": result["fabric_speedup_vs_vllm"],
            }
        )

    return {
        "name": NAME,
        "version": version(),
        "available": True,
        "equivalence": equivalence,
        "timings": timings,
    }
