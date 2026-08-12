"""Paged (indexed) recurrent state.

A serving host keeps recurrent state in a slot-addressed cache. The kernel reads
and writes those slots directly so a host needs no gather or scatter, which means
slot addressing has to be exactly right: a wrong offset would silently corrupt
another sequence's state.
"""

from __future__ import annotations

import pytest
import torch

from integration.dispatch import dispatch_gated_delta_decode
from kernels.gated_delta_decode import gated_delta_decode, torch_gated_delta_decode_reference

requires_cuda = pytest.mark.skipif(not torch.cuda.is_available(), reason="needs a CUDA GPU")

SEQUENCES, HEADS, KEY_DIM, VALUE_DIM, SLOTS = 4, 8, 32, 32, 10


def make_inputs(device: str, dtype: torch.dtype = torch.float32):
    generator = torch.Generator(device=device).manual_seed(7)
    q = torch.randn(SEQUENCES, HEADS, KEY_DIM, dtype=dtype, device=device, generator=generator)
    k = torch.randn_like(q)
    v = torch.randn(SEQUENCES, HEADS, VALUE_DIM, dtype=dtype, device=device, generator=generator)
    g = -torch.rand(SEQUENCES, HEADS, dtype=torch.float32, device=device, generator=generator)
    beta = torch.rand(SEQUENCES, HEADS, dtype=torch.float32, device=device, generator=generator)
    cache = (
        torch.randn(
            SLOTS, HEADS, KEY_DIM, VALUE_DIM,
            dtype=torch.float32, device=device, generator=generator,
        )
        * 0.05
    )
    # A shuffled, non-identity mapping: an off-by-one offset cannot pass.
    slots = torch.tensor([7, 2, 9, 0], dtype=torch.int32, device=device)
    return q, k, v, g, beta, cache, slots


def test_reference_paged_matches_reference_dense() -> None:
    q, k, v, g, beta, cache, slots = make_inputs("cpu")
    dense = cache[slots.long()].contiguous()

    paged_cache = cache.clone()
    paged_out = torch_gated_delta_decode_reference(
        q, k, v, g, beta, paged_cache, state_indices=slots
    )
    dense_out = torch_gated_delta_decode_reference(q, k, v, g, beta, dense)

    torch.testing.assert_close(paged_out, dense_out)
    torch.testing.assert_close(paged_cache[slots.long()], dense)


def test_reference_leaves_unaddressed_slots_untouched() -> None:
    q, k, v, g, beta, cache, slots = make_inputs("cpu")
    original = cache.clone()
    torch_gated_delta_decode_reference(q, k, v, g, beta, cache, state_indices=slots)

    untouched = [s for s in range(SLOTS) if s not in slots.tolist()]
    torch.testing.assert_close(cache[untouched], original[untouched])


def test_paged_state_validation() -> None:
    q, k, v, g, beta, cache, slots = make_inputs("cpu")

    with pytest.raises(ValueError, match="state_indices must have shape"):
        torch_gated_delta_decode_reference(
            q, k, v, g, beta, cache, state_indices=slots[:2]
        )
    with pytest.raises(TypeError, match="int32 or int64"):
        torch_gated_delta_decode_reference(
            q, k, v, g, beta, cache, state_indices=slots.float()
        )
    with pytest.raises(ValueError, match=r"paged state must have shape"):
        torch_gated_delta_decode_reference(
            q, k, v, g, beta, cache[:, :2], state_indices=slots
        )


def test_dense_state_shape_is_still_enforced() -> None:
    q, k, v, g, beta, cache, _slots = make_inputs("cpu")
    with pytest.raises(ValueError, match=r"state must have shape \[batch, heads"):
        torch_gated_delta_decode_reference(q, k, v, g, beta, cache)


@requires_cuda
def test_kernel_paged_matches_kernel_dense() -> None:
    q, k, v, g, beta, cache, slots = make_inputs("cuda", torch.float16)
    dense = cache[slots.long()].contiguous()

    paged_cache = cache.clone()
    paged_out = gated_delta_decode(q, k, v, g, beta, paged_cache, state_indices=slots)
    dense_out = gated_delta_decode(q, k, v, g, beta, dense)
    torch.cuda.synchronize()

    torch.testing.assert_close(paged_out, dense_out)
    torch.testing.assert_close(paged_cache[slots.long()], dense)


@requires_cuda
def test_kernel_leaves_unaddressed_slots_untouched() -> None:
    q, k, v, g, beta, cache, slots = make_inputs("cuda", torch.float16)
    original = cache.clone()
    gated_delta_decode(q, k, v, g, beta, cache, state_indices=slots)
    torch.cuda.synchronize()

    untouched = [s for s in range(SLOTS) if s not in slots.tolist()]
    torch.testing.assert_close(cache[untouched], original[untouched])


@requires_cuda
def test_kernel_paged_matches_the_eager_reference() -> None:
    q, k, v, g, beta, cache, slots = make_inputs("cuda", torch.float16)

    kernel_cache = cache.clone()
    kernel_out = gated_delta_decode(q, k, v, g, beta, kernel_cache, state_indices=slots)
    reference_cache = cache.clone()
    reference_out = torch_gated_delta_decode_reference(
        q, k, v, g, beta, reference_cache, state_indices=slots
    )
    torch.cuda.synchronize()

    torch.testing.assert_close(kernel_out, reference_out, atol=2e-3, rtol=2e-3)
    torch.testing.assert_close(kernel_cache, reference_cache, atol=2e-3, rtol=2e-3)


@requires_cuda
def test_dispatch_forwards_state_indices() -> None:
    q, k, v, g, beta, cache, slots = make_inputs("cuda", torch.float16)
    _out, decision = dispatch_gated_delta_decode(
        q, k, v, g, beta, cache, mode="fabric", state_indices=slots
    )
    assert decision.used_fabric


def test_dispatch_rejects_bad_indices_before_launching() -> None:
    q, k, v, g, beta, cache, slots = make_inputs("cpu")
    from integration.dispatch import unsupported_reason

    reason = unsupported_reason(q, k, v, g, beta, cache, state_indices=slots.float())
    assert reason is not None
