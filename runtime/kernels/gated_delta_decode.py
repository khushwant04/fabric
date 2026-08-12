"""Shape-generic single-token gated-delta recurrent update.

This prototype implements a single-token recurrent update while deliberately
remaining independent of any model or checkpoint identity.

Numerical contract: the Triton path computes Q/K L2 normalization in FP32,
while the authoritative eager fallback normalizes in the model dtype before
converting the recurrence to FP32. This is an intentional accuracy-oriented
runtime choice, not bitwise operation-order equivalence. Correctness must be
qualified by output/final-state tolerances and recurrent multi-step drift tests.

The Triton kernel fuses:
  * Q/K L2 normalization and query scaling
  * recurrent-state decay
  * memory prediction and beta correction
  * rank-1 recurrent-state update (in place)
  * output reduction

The recurrent state remains FP32, as in the authoritative eager fallback. Q, K,
and V may be FP16, BF16, or FP32. T4 deployments should use FP16 because SM75
does not provide native BF16 arithmetic; A10 can use BF16.
"""

from __future__ import annotations

import torch
import triton
import triton.language as tl

L2_EPS = 1e-6
_MAX_KEY_DIM = 256


@triton.jit
def _gated_delta_decode_kernel(
    q_ptr,
    k_ptr,
    v_ptr,
    g_ptr,
    beta_ptr,
    state_ptr,
    out_ptr,
    state_index_ptr,
    heads: tl.constexpr,
    key_dim: tl.constexpr,
    value_dim: tl.constexpr,
    BLOCK_K: tl.constexpr,
    BLOCK_V: tl.constexpr,
    QUERY_SCALE: tl.constexpr,
    L2_EPS: tl.constexpr,
    HAS_STATE_INDEX: tl.constexpr,
):
    """One program updates one value-dimension tile for one batch/head pair.

    With ``HAS_STATE_INDEX`` the recurrent state is a paged cache and each
    sequence's slot is read from ``state_index_ptr``, which lets a serving host
    pass its cache directly instead of gathering rows into a dense tensor.
    """
    batch_head = tl.program_id(0)
    value_block = tl.program_id(1)

    key_offsets = tl.arange(0, BLOCK_K)
    key_mask = key_offsets < key_dim
    value_offsets = value_block * BLOCK_V + tl.arange(0, BLOCK_V)
    value_mask = value_offsets < value_dim

    q_offsets = batch_head * key_dim + key_offsets
    q = tl.load(q_ptr + q_offsets, mask=key_mask, other=0.0).to(tl.float32)
    k = tl.load(k_ptr + q_offsets, mask=key_mask, other=0.0).to(tl.float32)

    q_inv_norm = tl.rsqrt(tl.sum(q * q, axis=0) + L2_EPS)
    k_inv_norm = tl.rsqrt(tl.sum(k * k, axis=0) + L2_EPS)
    q = q * q_inv_norm * QUERY_SCALE
    k = k * k_inv_norm

    scalar_offset = batch_head
    decay = tl.exp(tl.load(g_ptr + scalar_offset).to(tl.float32))
    beta = tl.load(beta_ptr + scalar_offset).to(tl.float32)

    if HAS_STATE_INDEX:
        sequence = batch_head // heads
        head = batch_head % heads
        slot = tl.load(state_index_ptr + sequence).to(tl.int64)
        state_base = (slot * heads + head) * key_dim * value_dim
    else:
        state_base = batch_head * key_dim * value_dim
    state_offsets = state_base + key_offsets[:, None] * value_dim + value_offsets[None, :]
    state_mask = key_mask[:, None] & value_mask[None, :]
    state = tl.load(state_ptr + state_offsets, mask=state_mask, other=0.0).to(tl.float32)
    state = state * decay

    memory = tl.sum(state * k[:, None], axis=0)
    value = tl.load(
        v_ptr + batch_head * value_dim + value_offsets,
        mask=value_mask,
        other=0.0,
    ).to(tl.float32)
    delta = (value - memory) * beta

    state = state + k[:, None] * delta[None, :]
    output = tl.sum(state * q[:, None], axis=0)

    tl.store(state_ptr + state_offsets, state, mask=state_mask)
    tl.store(
        out_ptr + batch_head * value_dim + value_offsets,
        output,
        mask=value_mask,
    )


def _storage_interval(tensor: torch.Tensor) -> tuple[int, int]:
    """Return the byte interval occupied by a contiguous tensor."""
    start = tensor.untyped_storage().data_ptr() + tensor.storage_offset() * tensor.element_size()
    return start, start + tensor.numel() * tensor.element_size()


def _overlaps(left: torch.Tensor, right: torch.Tensor) -> bool:
    left_start, left_end = _storage_interval(left)
    right_start, right_end = _storage_interval(right)
    return max(left_start, right_start) < min(left_end, right_end)


def _validate_inputs(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    g: torch.Tensor,
    beta: torch.Tensor,
    state: torch.Tensor,
    out: torch.Tensor | None,
    *,
    require_cuda: bool = True,
    state_indices: torch.Tensor | None = None,
) -> tuple[int, int, int, int]:
    """Validate the shared operand contract.

    ``require_cuda`` is set for the Triton path, which can only run on a GPU. The
    eager reference is pure PyTorch, so it accepts CPU tensors and can therefore
    serve as a fallback wherever the fused kernel cannot run.

    With ``state_indices`` the state is a paged cache ``[slots, heads, K, V]`` and
    each sequence's slot is looked up, matching how a serving host stores
    recurrent state.
    """
    if require_cuda and not q.is_cuda:
        raise ValueError("all inputs must be CUDA tensors")
    if q.ndim != 3:
        raise ValueError(f"q must have shape [batch, heads, key_dim], got {tuple(q.shape)}")

    batch, heads, key_dim = q.shape
    if batch <= 0 or heads <= 0 or key_dim <= 0:
        raise ValueError("batch, heads, and key_dim must be positive")
    if key_dim > _MAX_KEY_DIM:
        raise ValueError(f"key_dim must not exceed {_MAX_KEY_DIM}")
    if tuple(k.shape) != tuple(q.shape):
        raise ValueError("k must match q shape [batch, heads, key_dim]")
    if v.ndim != 3 or tuple(v.shape[:2]) != (batch, heads) or v.shape[-1] <= 0:
        raise ValueError("v must have shape [batch, heads, value_dim] with value_dim > 0")

    value_dim = v.shape[-1]
    if state_indices is None:
        if tuple(state.shape) != (batch, heads, key_dim, value_dim):
            raise ValueError("state must have shape [batch, heads, key_dim, value_dim]")
    else:
        if state.ndim != 4 or tuple(state.shape[1:]) != (heads, key_dim, value_dim):
            raise ValueError(
                "paged state must have shape [slots, heads, key_dim, value_dim]"
            )
        if state_indices.ndim != 1 or state_indices.shape[0] != batch:
            raise ValueError("state_indices must have shape [batch]")
        if state_indices.dtype not in (torch.int32, torch.int64):
            raise TypeError("state_indices must be int32 or int64")
        if not state_indices.is_contiguous():
            raise ValueError("state_indices must be contiguous")
        if state_indices.device != q.device:
            raise ValueError("state_indices must be on the same device as the inputs")
    if tuple(g.shape) != (batch, heads) or tuple(beta.shape) != (batch, heads):
        raise ValueError("g and beta must have shape [batch, heads]")
    if state.dtype != torch.float32:
        raise TypeError("the recurrent state must be FP32")
    if q.dtype not in (torch.float16, torch.bfloat16, torch.float32):
        raise TypeError("q, k, and v must be FP16, BF16, or FP32")
    if k.dtype != q.dtype or v.dtype != q.dtype:
        raise TypeError("q, k, and v must have the same dtype")

    tensors = (q, k, v, g, beta, state)
    if any(t.device != q.device for t in tensors):
        raise ValueError("all inputs must be on the same device")
    if any(not t.is_contiguous() for t in tensors):
        raise ValueError("all inputs must be contiguous")
    if any(_overlaps(state, tensor) for tensor in (q, k, v, g, beta)):
        raise ValueError("state must not overlap q, k, v, g, or beta")
    if out is not None:
        if out.shape != v.shape or out.dtype != v.dtype or out.device != v.device:
            raise ValueError("out must match v shape, dtype, and device")
        if not out.is_contiguous():
            raise ValueError("out must be contiguous")
        if any(_overlaps(out, tensor) for tensor in tensors):
            raise ValueError("out must not overlap any input or the in-place state")

    return batch, heads, key_dim, value_dim


def gated_delta_decode(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    g: torch.Tensor,
    beta: torch.Tensor,
    state: torch.Tensor,
    *,
    out: torch.Tensor | None = None,
    block_v: int = 32,
    state_indices: torch.Tensor | None = None,
) -> torch.Tensor:
    """Run the fused single-token gated-delta recurrence and update ``state`` in place.

    The caller must provide exclusive ownership of ``state`` for the duration of
    the launch. Output/input aliasing is rejected because each value tile reads
    the complete Q/K vectors while peer tiles write their outputs.

    ``state_indices`` selects one slot per sequence from a paged state cache, so a
    serving host can pass its cache directly rather than gathering rows into a
    dense tensor and scattering them back.
    """
    batch, heads, key_dim, value_dim = _validate_inputs(
        q, k, v, g, beta, state, out, state_indices=state_indices
    )
    if block_v not in (8, 16, 32):
        raise ValueError("block_v must be one of 8, 16, or 32")
    if out is None:
        out = torch.empty_like(v)

    block_k = 1 << (key_dim - 1).bit_length()
    grid = (batch * heads, triton.cdiv(value_dim, block_v))
    _gated_delta_decode_kernel[grid](
        q,
        k,
        v,
        g,
        beta,
        state,
        out,
        state_indices,
        heads,
        key_dim,
        value_dim,
        BLOCK_K=block_k,
        BLOCK_V=block_v,
        QUERY_SCALE=key_dim**-0.5,
        L2_EPS=L2_EPS,
        HAS_STATE_INDEX=state_indices is not None,
        num_warps=4 if block_v <= 16 else 8,
    )
    return out


def torch_gated_delta_decode_reference(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    g: torch.Tensor,
    beta: torch.Tensor,
    state: torch.Tensor,
    *,
    out: torch.Tensor | None = None,
    state_indices: torch.Tensor | None = None,
) -> torch.Tensor:
    """Eager reference preserving the authoritative operation order.

    Runs on any device, so a host can fall back to it when the fused kernel
    cannot serve a call. With ``state_indices`` the addressed slots of a paged
    cache are read, updated, and written back, matching the fused path's effect
    on the caller's cache.
    """
    _, _, key_dim, _ = _validate_inputs(
        q, k, v, g, beta, state, out, require_cuda=False, state_indices=state_indices
    )

    slots = None if state_indices is None else state_indices.long()
    working = state if slots is None else state[slots]

    # The authoritative fallback applies l2norm in the model dtype, then
    # converts Q/K/V/beta/g to FP32 for the recurrent update.
    q_norm = q * torch.rsqrt((q * q).sum(dim=-1, keepdim=True) + L2_EPS)
    k_norm = k * torch.rsqrt((k * k).sum(dim=-1, keepdim=True) + L2_EPS)
    q_float = q_norm.float() * (key_dim**-0.5)
    k_float = k_norm.float()

    working.mul_(g.float().exp().unsqueeze(-1).unsqueeze(-1))
    memory = (working * k_float.unsqueeze(-1)).sum(dim=-2)
    delta = (v.float() - memory) * beta.float().unsqueeze(-1)
    working.add_(k_float.unsqueeze(-1) * delta.unsqueeze(-2))
    result = (working * q_float.unsqueeze(-1)).sum(dim=-2)

    if slots is not None:
        # ``state[slots]`` gathered a copy, so the cache needs the write back.
        state[slots] = working

    if out is None:
        return result.to(v.dtype)
    out.copy_(result)
    return out


__all__ = [
    "gated_delta_decode",
    "torch_gated_delta_decode_reference",
]
