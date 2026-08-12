"""Fabric substitution for vLLM's gated-delta decode.

The gating checks are metadata-only, so most of this runs without a GPU. The
equivalence test needs CUDA and vLLM's kernel.
"""

from __future__ import annotations

import pytest
import torch

from fabric_serving.vllm_gated_delta import (
    SubstitutionStats,
    fabric_fused_recurrent_gated_delta_rule,
    unsupported_call_reason,
)

requires_cuda = pytest.mark.skipif(not torch.cuda.is_available(), reason="needs a CUDA GPU")

HEADS, KEY_DIM, VALUE_DIM = 4, 16, 16


def supported_call(sequences: int = 3, device: str = "cpu") -> dict:
    """A call shaped exactly like vLLM's single-token decode."""
    shape = (1, sequences, HEADS)
    return {
        "q": torch.randn(*shape, KEY_DIM, device=device),
        "k": torch.randn(*shape, KEY_DIM, device=device),
        "v": torch.randn(*shape, VALUE_DIM, device=device),
        "g": -torch.rand(*shape, device=device),
        "beta": torch.rand(*shape, device=device),
        "initial_state": torch.zeros(8, HEADS, KEY_DIM, VALUE_DIM, device=device),
        "ssm_state_indices": torch.arange(sequences, dtype=torch.int32, device=device),
        "cu_seqlens": torch.arange(sequences + 1, dtype=torch.int32, device=device),
        "num_accepted_tokens": None,
        "use_qk_l2norm_in_kernel": True,
        "inplace_final_state": True,
    }


#: Keys the metadata-only gating check accepts, a subset of the adapter's kwargs.
GATING_KEYS = (
    "q",
    "beta",
    "initial_state",
    "ssm_state_indices",
    "cu_seqlens",
    "num_accepted_tokens",
    "use_qk_l2norm_in_kernel",
    "inplace_final_state",
)


def gating(call: dict, **extra) -> str | None:
    return unsupported_call_reason(**{key: call[key] for key in GATING_KEYS}, **extra)


def test_single_token_decode_is_supported() -> None:
    assert gating(supported_call()) is None


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"use_qk_l2norm_in_kernel": False}, "normalization outside"),
        ({"inplace_final_state": False}, "freshly allocated"),
        ({"beta": None}, "beta is required"),
        ({"initial_state": None}, "paged state cache"),
        ({"ssm_state_indices": None}, "paged state cache"),
        ({"cu_seqlens": None}, "cu_seqlens is required"),
    ],
)
def test_unsupported_shapes_are_named(override, expected) -> None:
    reason = gating({**supported_call(), **override})
    assert reason is not None and expected in reason


def test_speculative_decoding_is_not_claimed() -> None:
    call = supported_call()
    call["num_accepted_tokens"] = torch.ones(3, dtype=torch.int32)
    reason = gating(call)
    assert reason is not None and "speculative" in reason


def test_multi_token_steps_are_not_claimed() -> None:
    """Prefill and chunked steps carry more tokens than sequences."""
    call = supported_call(sequences=3)
    call["q"] = torch.randn(1, 6, HEADS, KEY_DIM)
    reason = gating(call)
    assert reason is not None and "more than one token per sequence" in reason


def test_unexpected_rank_is_rejected() -> None:
    call = supported_call()
    call["q"] = torch.randn(3, HEADS, KEY_DIM)
    reason = gating(call)
    assert reason is not None and "tokens, heads, dim" in reason


def test_gating_never_reads_device_values() -> None:
    """A per-call sync cost 0.2 ms, twenty times the kernel being dispatched.

    Patching ``torch.all`` to explode proves the fast path never inspects
    ``cu_seqlens`` contents.
    """
    original_all = torch.all

    def exploding_all(*_args, **_kwargs):
        raise AssertionError("gating must not read device values")

    torch.all = exploding_all
    try:
        assert gating(supported_call()) is None
        # The opt-in verification path is allowed to sync.
        with pytest.raises(AssertionError, match="must not read"):
            gating(supported_call(), verify_lengths=True)
    finally:
        torch.all = original_all


def test_verify_lengths_catches_a_ragged_batch() -> None:
    call = supported_call(sequences=2)
    # Two sequences, two tokens, but lengths are 0 and 2 rather than 1 and 1.
    call["cu_seqlens"] = torch.tensor([0, 0, 2], dtype=torch.int32)
    assert gating(call) is None
    assert "not all one" in gating(call, verify_lengths=True)


def test_unsupported_calls_delegate_to_vllm() -> None:
    call = supported_call()
    call["use_qk_l2norm_in_kernel"] = False
    seen: list[dict] = []
    stats = SubstitutionStats()

    def fake_vllm(**kwargs):
        seen.append(kwargs)
        return "delegated", kwargs["initial_state"]

    result, _state = fabric_fused_recurrent_gated_delta_rule(
        **call, fallback=fake_vllm, stats=stats
    )
    assert result == "delegated"
    assert seen and seen[0]["use_qk_l2norm_in_kernel"] is False
    assert stats.delegated_calls == 1
    assert stats.fabric_calls == 0
    assert "normalization outside" in stats.last_delegation_reason


def test_missing_fallback_is_an_explicit_error() -> None:
    call = supported_call()
    call["beta"] = None
    with pytest.raises(NotImplementedError, match="no vLLM fallback"):
        fabric_fused_recurrent_gated_delta_rule(**call)


def test_stats_snapshot_reports_the_fabric_fraction() -> None:
    stats = SubstitutionStats(fabric_calls=3, delegated_calls=1)
    snapshot = stats.snapshot()
    assert snapshot["fabric_fraction"] == 0.75
    assert SubstitutionStats().snapshot()["fabric_fraction"] == 0.0


@requires_cuda
def test_matches_vllm_on_identical_inputs() -> None:
    """Equivalence against vLLM's own kernel, with a shuffled slot table."""
    from fabric_serving.compare import compare, make_decode_batch

    for sequences in (1, 4, 16):
        batch = make_decode_batch(sequences, 8, 64, 64, slots=64, dtype=torch.float16)
        result = compare(batch, warmup=5, reps=20)
        assert result["output_max_abs_vs_vllm"] <= 2e-3
        assert result["cache_max_abs_vs_vllm"] <= 2e-3
        assert result["sequences"] == sequences


@requires_cuda
def test_fabric_mode_serves_the_decode_path() -> None:
    from fabric_serving.compare import make_decode_batch

    batch = make_decode_batch(4, 8, 64, 64, slots=32, dtype=torch.float16)
    stats = SubstitutionStats()
    out, state = fabric_fused_recurrent_gated_delta_rule(
        q=batch["q"],
        k=batch["k"],
        v=batch["v"],
        g=batch["g"],
        beta=batch["beta"],
        scale=batch["scale"],
        initial_state=batch["cache"],
        inplace_final_state=True,
        cu_seqlens=batch["cu_seqlens"],
        ssm_state_indices=batch["indices"],
        use_qk_l2norm_in_kernel=True,
        mode="fabric",
        stats=stats,
    )
    assert tuple(out.shape) == (1, 4, 8, 64)
    # The cache is updated in place, so the returned state is the caller's cache.
    assert state.data_ptr() == batch["cache"].data_ptr()
    assert stats.fabric_calls == 1
    assert stats.delegated_calls == 0
