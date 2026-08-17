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
    from fabric_serving.vllm_ops import resolve

    ops = resolve()
    return None if ops is None else ops.version


def is_available() -> bool:
    """Whether the comparison can run in this environment."""
    from fabric_serving.vllm_ops import resolve

    return resolve() is not None


def _agreement_with_reference(
    *, heads: int, key_dim: int, value_dim: int, dtype: Any, slots: int
) -> dict[str, Any]:
    """How far vLLM's op is from the delta rule the Fabric kernel implements.

    A speedup is only meaningful between implementations of the same function. The
    version this project pinned first agreed with the eager reference to fp16
    resolution; a later version's unfused op does not, and the gap widens with
    concurrency, so a timing ratio against it would compare different computations
    and read as a win.

    Recorded per environment rather than assumed, because which vLLM is installed
    decides the answer.
    """
    import torch
    from kernels.gated_delta_decode import torch_gated_delta_decode_reference as reference

    from fabric_serving.compare import _call_vllm, make_decode_batch

    def flatten(x):
        if x.dim() == 4:
            return x.reshape(-1, *x.shape[-2:])
        return x.reshape(-1, x.shape[-1])

    def l2(x):
        return torch.nn.functional.normalize(x.float(), dim=-1, p=2).to(x.dtype)

    deltas = {}
    for count in (1, 16):
        torch.manual_seed(0)
        batch = make_decode_batch(
            sequences=count, heads=heads, key_dim=key_dim,
            value_dim=value_dim, slots=slots, dtype=dtype,
        )
        q, k, v, g, beta = (flatten(batch[name]) for name in ("q", "k", "v", "g", "beta"))
        expected = reference(
            l2(q), l2(k), v, g, beta, batch["cache"].clone(), state_indices=batch["indices"]
        )
        actual = _call_vllm(batch, batch["cache"].clone())
        deltas[count] = (
            expected.float().reshape(-1) - actual.float().reshape(-1)
        ).abs().max().item()

    # fp16 inputs, so agreement is around 1e-3; an order of magnitude beyond that is a
    # different computation rather than rounding.
    worst = max(deltas.values())
    return {
        "max_abs_vs_eager_reference": worst,
        "per_sequence_count": {str(count): delta for count, delta in deltas.items()},
        "implements_same_function": worst < 5e-3,
    }


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

    from fabric_serving.vllm_ops import resolve

    ops = resolve()
    agreement = _agreement_with_reference(
        heads=heads, key_dim=key_dim, value_dim=value_dim, dtype=dtype, slots=slots
    )

    return {
        "name": NAME,
        "version": version(),
        "available": True,
        # Which module the baseline came from, because it moves between versions and
        # the numbers are only attributable with it.
        "module": None if ops is None else ops.module,
        "has_packed_decode_path": bool(ops and ops.has_packed_decode),
        # Whether a timing ratio against this op is even meaningful.
        "reference_agreement": agreement,
        "equivalence": equivalence,
        "timings": timings,
    }
