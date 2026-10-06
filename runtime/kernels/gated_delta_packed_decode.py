"""Fused single-token gated-delta decode over vLLM's packed QKV layout.

The kernel in :mod:`kernels.gated_delta_decode` implements the recurrence alone, with
gating and normalisation already applied by the caller. That is the operation vLLM 0.11.0
exposed, and it is not the operation a current vLLM actually calls during decode. Newer
versions call one kernel that additionally unpacks a combined QKV tensor, L2-normalises q
and k, derives the decay from ``A_log``/``dt_bias`` and the write strength from ``b``, and
gathers and scatters the recurrent state through an index table.

Substituting the unfused kernel into that path is possible only by doing the fused work in
separate launches around it, which adds traffic and launch overhead to win back none. So
this kernel does the whole thing in one launch, which is what makes a substitution in a
live host meaningful rather than a demonstration.

What the arithmetic is bound by is worth stating plainly, because it sets what any
implementation can achieve. Every step reads the whole ``[V, K]`` state for each value
head and writes it back: at the launch model's shapes that is 128*128*4*2 bytes per head,
about 2 MB per token across 16 heads. Nothing about the algorithm can avoid touching all
of it, so at equal precision two correct kernels converge on the same bandwidth limit.
The room left is in what surrounds it: how many times q and k are read, how often the
transcendental gate maths is repeated, and how the launch is shaped.

This kernel takes the room that exists. vLLM's kernel splits the value dimension into
tiles of at most 32 and recomputes the gate in every tile, so at V=128 it evaluates the
same exp, log and sigmoid four times per head and re-reads q and k four times. Here the
tile is configurable, and when it spans V the gate is computed once and q and k are read
once. Whether that is worth anything is a question for measurement on the target GPU, and
the answer differs between one sequence and many: fewer, larger programs leave a small GPU
with less to overlap.
"""

from __future__ import annotations

import math

import torch
import triton
import triton.language as tl

#: Above this the softplus is its own argument to within float32 resolution, and the
#: exp overflows. vLLM uses the same threshold, so switching over at the same point keeps
#: the two implementations bit-comparable rather than merely close.
SOFTPLUS_THRESHOLD = 20.0

#: Matches the epsilon inside vLLM's normalisation. A different one would produce answers
#: that differ from the host's own kernel for reasons unrelated to the recurrence.
L2NORM_EPS = 1e-6

#: State index reserved to mean "no state". vLLM writes zeros to the output and leaves the
#: state untouched, which is what a sequence that has not begun should produce.
NULL_STATE_INDEX = 0


@triton.jit
def _fused_packed_decode_kernel(
    mixed_qkv,
    a,
    b,
    A_log,
    dt_bias,
    o,
    h0,
    ht,
    ssm_state_indices,
    scale,
    # Compile-time, as vLLM declares them: with the strides known the address arithmetic
    # folds into constants instead of being recomputed per access. Leaving them as runtime
    # arguments cost more than every saving this kernel makes.
    stride_qkv_token: tl.constexpr,
    stride_a_token: tl.constexpr,
    stride_b_token: tl.constexpr,
    stride_h0_token: tl.constexpr,
    stride_ht_token: tl.constexpr,
    stride_indices: tl.constexpr,
    H: tl.constexpr,
    HV: tl.constexpr,
    K: tl.constexpr,
    V: tl.constexpr,
    BK: tl.constexpr,
    BV: tl.constexpr,
    THRESHOLD: tl.constexpr,
    USE_QK_L2NORM: tl.constexpr,
):
    i_v, i_nh = tl.program_id(0), tl.program_id(1)
    i_n, i_hv = i_nh // HV, i_nh % HV
    # Grouped heads: several value heads share one query/key head.
    i_h = i_hv // (HV // H)

    o_k = tl.arange(0, BK)
    o_v = i_v * BV + tl.arange(0, BV)
    mask_k = o_k < K
    mask_v = o_v < V
    mask_state = mask_v[:, None] & mask_k[None, :]

    state_index = tl.load(ssm_state_indices + i_n * stride_indices).to(tl.int64)
    p_o = o + (i_n * HV + i_hv) * V + o_v

    if state_index <= 0:
        # A sequence with no state yet contributes nothing. Written rather than skipped,
        # because the output buffer is not zeroed by the caller.
        tl.store(p_o, tl.zeros([BV], dtype=tl.float32).to(p_o.dtype.element_ty), mask=mask_v)
        return

    p_state = h0 + state_index * stride_h0_token + i_hv * V * K
    p_state = p_state + o_v[:, None] * K + o_k[None, :]
    state = tl.load(p_state, mask=mask_state, other=0).to(tl.float32)

    p_mixed = mixed_qkv + i_n * stride_qkv_token
    # Packed as [q (H*K) | k (H*K) | v (HV*V)].
    q = tl.load(p_mixed + i_h * K + o_k, mask=mask_k, other=0).to(tl.float32)
    k = tl.load(p_mixed + H * K + i_h * K + o_k, mask=mask_k, other=0).to(tl.float32)
    v = tl.load(p_mixed + 2 * H * K + i_hv * V + o_v, mask=mask_v, other=0).to(tl.float32)

    if USE_QK_L2NORM:
        q = q / tl.sqrt(tl.sum(q * q) + 1e-6)
        k = k / tl.sqrt(tl.sum(k * k) + 1e-6)
    q = q * scale

    # Computed once per program. Where the tile spans V this is once per head per step
    # rather than once per tile, which is the redundancy a 32-wide tile pays at V=128.
    a_value = tl.load(a + i_n * stride_a_token + i_hv).to(tl.float32)
    b_value = tl.load(b + i_n * stride_b_token + i_hv).to(tl.float32)
    A_log_value = tl.load(A_log + i_hv).to(tl.float32)
    dt_bias_value = tl.load(dt_bias + i_hv).to(tl.float32)

    x = a_value + dt_bias_value
    softplus_x = tl.where(x <= THRESHOLD, tl.log(1.0 + tl.exp(x)), x)
    decay = -tl.exp(A_log_value) * softplus_x
    # vLLM rounds the sigmoid to b's dtype before using it in the FP32 recurrence.
    # Retaining FP32 here changes every next-state update when b is FP16/BF16.
    write_strength = tl.sigmoid(b_value).to(b.dtype.element_ty).to(tl.float32)

    # The delta rule: decay the state, subtract what it already predicts for this key,
    # scale that error by the write strength, and accumulate it against the key.
    state *= tl.exp(decay)
    v -= tl.sum(state * k[None, :], 1)
    v *= write_strength
    state += v[:, None] * k[None, :]

    tl.store(p_o, tl.sum(state * q[None, :], 1).to(p_o.dtype.element_ty), mask=mask_v)

    p_final = ht + state_index * stride_ht_token + i_hv * V * K
    p_final = p_final + o_v[:, None] * K + o_k[None, :]
    tl.store(p_final, state.to(p_final.dtype.element_ty), mask=mask_state)


def validate_packed_inputs(
    mixed_qkv: torch.Tensor,
    a: torch.Tensor,
    b: torch.Tensor,
    A_log: torch.Tensor,
    dt_bias: torch.Tensor,
    scale: float,
    initial_state: torch.Tensor,
    out: torch.Tensor,
    ssm_state_indices: torch.Tensor,
) -> tuple[int, int, int, int, int]:
    """Check host-visible metadata before any in-place kernel launch.

    Outer token/slot strides may contain padding; the packed inner dimensions may
    not. Device index values remain vLLM's responsibility, avoiding a per-token
    synchronization in serving. The reference checks their bounds separately.
    """
    tensors = (mixed_qkv, a, b, A_log, dt_bias, initial_state, out, ssm_state_indices)
    if any(tensor.device != mixed_qkv.device for tensor in tensors):
        raise ValueError("all packed decode inputs must be on the same device")
    supported = (torch.float16, torch.bfloat16, torch.float32)
    if any(tensor.dtype not in supported for tensor in tensors[:5]) or out.dtype not in supported:
        raise ValueError("packed decode floating inputs must be FP16, BF16, or FP32")
    if initial_state.dtype != torch.float32:
        raise ValueError("packed recurrent state must be FP32")
    if ssm_state_indices.dtype not in (torch.int32, torch.int64):
        raise ValueError("ssm_state_indices must be int32 or int64")
    if not isinstance(scale, int | float) or not math.isfinite(scale) or scale <= 0:
        raise ValueError("scale must be finite and positive")
    if mixed_qkv.ndim != 2 or mixed_qkv.stride(-1) != 1:
        raise ValueError("mixed_qkv must be 2D and contiguous in its last dimension")
    if initial_state.ndim != 4:
        raise ValueError("initial_state must be 4D")
    batch = mixed_qkv.shape[0]
    HV, V, K = initial_state.shape[-3:]
    if min(initial_state.shape) <= 0 or max(K, V) > 256:
        raise ValueError("state dimensions must be positive and K/V at most 256")
    if (
        initial_state.stride(-1) != 1
        or initial_state.stride(-2) != K
        or initial_state.stride(-3) != V * K
    ):
        raise ValueError("state head/value/key dimensions must be contiguous")
    if not out.is_contiguous() or out.shape != (batch, 1, HV, V):
        raise ValueError(f"out must be contiguous with shape {(batch, 1, HV, V)}")
    if a.shape != (batch, HV) or b.shape != (batch, HV) or a.stride(-1) != 1 or b.stride(-1) != 1:
        raise ValueError(f"a and b must be [{batch}, {HV}] with contiguous heads")
    if A_log.ndim != 1 or dt_bias.ndim != 1 or A_log.shape != (HV,) or dt_bias.shape != (HV,):
        raise ValueError(f"A_log and dt_bias must be 1D with {HV} elements")
    if A_log.stride(0) != 1 or dt_bias.stride(0) != 1:
        raise ValueError("A_log and dt_bias must be contiguous")
    if ssm_state_indices.shape != (batch,):
        raise ValueError(f"ssm_state_indices must be [{batch}]")
    qk_dim = mixed_qkv.shape[1] - HV * V
    if qk_dim <= 0 or qk_dim % (2 * K) != 0:
        raise ValueError("packed width is not q|k|v for this state geometry")
    H = qk_dim // (2 * K)
    if H <= 0 or HV % H != 0:
        raise ValueError(f"inferred head counts do not group: H={H}, HV={HV}")
    return batch, H, HV, K, V


def torch_packed_decode_reference(
    mixed_qkv: torch.Tensor,
    a: torch.Tensor,
    b: torch.Tensor,
    A_log: torch.Tensor,
    dt_bias: torch.Tensor,
    scale: float,
    initial_state: torch.Tensor,
    out: torch.Tensor,
    ssm_state_indices: torch.Tensor,
    use_qk_l2norm_in_kernel: bool = False,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Independent eager oracle for packed decode, including complete next state.

    This is a correctness oracle, not a serving fallback. It reads index values
    and synchronizes CUDA. Null/nonpositive indices produce zero output and leave
    every state slot untouched. Positive indices must be unique and in bounds.
    """
    batch, H, HV, K, V = validate_packed_inputs(
        mixed_qkv,
        a,
        b,
        A_log,
        dt_bias,
        scale,
        initial_state,
        out,
        ssm_state_indices,
    )
    valid = ssm_state_indices > 0
    slots = ssm_state_indices[valid].long()
    if slots.numel() and (
        slots.max().item() >= initial_state.shape[0] or slots.unique().numel() != slots.numel()
    ):
        raise ValueError("positive state indices must be unique and in bounds")
    out.zero_()
    if not slots.numel():
        return out, initial_state
    q, k, v = torch.split(mixed_qkv, [H * K, H * K, HV * V], dim=-1)
    q = q.reshape(batch, H, K)[valid].float().repeat_interleave(HV // H, dim=1)
    k = k.reshape(batch, H, K)[valid].float().repeat_interleave(HV // H, dim=1)
    v = v.reshape(batch, HV, V)[valid].float()
    if use_qk_l2norm_in_kernel:
        q = q / torch.sqrt((q * q).sum(-1, keepdim=True) + L2NORM_EPS)
        k = k / torch.sqrt((k * k).sum(-1, keepdim=True) + L2NORM_EPS)
    q = q * scale
    x = a[valid].float() + dt_bias.float()
    softplus = torch.where(x <= SOFTPLUS_THRESHOLD, torch.log(1.0 + torch.exp(x)), x)
    decay = torch.exp(-torch.exp(A_log.float()) * softplus)
    strength = torch.sigmoid(b[valid].float()).to(b.dtype).float()
    state = initial_state[slots].float() * decay[..., None, None]
    delta = (v - (state * k[..., None, :]).sum(-1)) * strength[..., None]
    state = state + delta[..., None] * k[..., None, :]
    out[valid, 0] = (state * q[..., None, :]).sum(-1).to(out.dtype)
    initial_state.index_copy_(0, slots, state)
    return out, initial_state


def fused_packed_decode(
    mixed_qkv: torch.Tensor,
    a: torch.Tensor,
    b: torch.Tensor,
    A_log: torch.Tensor,
    dt_bias: torch.Tensor,
    scale: float,
    initial_state: torch.Tensor,
    out: torch.Tensor,
    ssm_state_indices: torch.Tensor,
    use_qk_l2norm_in_kernel: bool = False,
    *,
    block_v: int | None = None,
    num_warps: int | None = None,
    num_stages: int = 3,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Run one decode step, writing into ``out`` and ``initial_state`` in place.

    Argument order and meaning match vLLM's packed decode op exactly, so this can be
    substituted for it without a shim. ``block_v``, ``num_warps`` and ``num_stages`` are
    exposed because the best shape is a property of the GPU and the batch, not of the
    algorithm, and picking one by reasoning rather than measurement is how a kernel ends
    up slower than the one it replaces.
    """
    batch, H, HV, K, V = validate_packed_inputs(
        mixed_qkv,
        a,
        b,
        A_log,
        dt_bias,
        scale,
        initial_state,
        out,
        ssm_state_indices,
    )
    if not mixed_qkv.is_cuda:
        raise ValueError("fused packed decode requires a CUDA device")
    if batch == 0:
        return out, initial_state

    BK = triton.next_power_of_2(K)
    if triton.cdiv(K, BK) != 1:
        raise ValueError(f"K={K} needs more than one key block, which this kernel does not split")

    # The default matches vLLM's, so an unconfigured call is a like-for-like comparison
    # rather than an accidental one.
    BV = block_v if block_v is not None else min(triton.next_power_of_2(V), 32)
    if not isinstance(BV, int) or BV <= 0 or BV & (BV - 1):
        raise ValueError("block_v must be a positive power of two")
    if BV > triton.next_power_of_2(V):
        BV = triton.next_power_of_2(V)
    warps = num_warps if num_warps is not None else max(1, BV // 32)
    if warps not in (1, 2, 4, 8):
        raise ValueError("num_warps must be 1, 2, 4, or 8")
    if not isinstance(num_stages, int) or not 1 <= num_stages <= 6:
        raise ValueError("num_stages must be between 1 and 6")

    grid = (triton.cdiv(V, BV), batch * HV)
    _fused_packed_decode_kernel[grid](
        mixed_qkv,
        a,
        b,
        A_log,
        dt_bias,
        out,
        initial_state,
        initial_state,
        ssm_state_indices,
        scale,
        mixed_qkv.stride(0),
        a.stride(0),
        b.stride(0),
        initial_state.stride(0),
        initial_state.stride(0),
        ssm_state_indices.stride(0),
        H=H,
        HV=HV,
        K=K,
        V=V,
        BK=BK,
        BV=BV,
        THRESHOLD=SOFTPLUS_THRESHOLD,
        USE_QK_L2NORM=use_qk_l2norm_in_kernel,
        num_warps=warps,
        num_stages=num_stages,
    )
    return out, initial_state
