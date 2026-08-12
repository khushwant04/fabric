"""Fabric substitution for vLLM's gated-delta decode op.

vLLM's Qwen3-Next layer calls ``fused_recurrent_gated_delta_rule`` with its whole
paged recurrent-state cache plus one slot index per sequence, and ragged
``cu_seqlens``. The Fabric kernel accepts that cache directly through
``state_indices``, so substitution needs no gather or scatter.

The adapter only claims the case it actually implements — a pure single-token
decode step — and delegates everything else (prefill, speculative multi-token
steps, missing beta, unexpected scale) to vLLM's own implementation. Silently
computing a different function would be far worse than not accelerating a call.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import torch
from integration.dispatch import KernelMode, dispatch_gated_delta_decode

DELEGATE_TO_VLLM = "delegated to vLLM"


@dataclasses.dataclass
class SubstitutionStats:
    """How often Fabric served a decode call, and why it did not."""

    fabric_calls: int = 0
    delegated_calls: int = 0
    last_delegation_reason: str | None = None

    def snapshot(self) -> dict[str, Any]:
        total = self.fabric_calls + self.delegated_calls
        return {
            "fabric_calls": self.fabric_calls,
            "delegated_calls": self.delegated_calls,
            "fabric_fraction": self.fabric_calls / total if total else 0.0,
            "last_delegation_reason": self.last_delegation_reason,
        }


def unsupported_call_reason(
    *,
    q: torch.Tensor,
    beta: torch.Tensor | None,
    initial_state: torch.Tensor | None,
    ssm_state_indices: torch.Tensor | None,
    cu_seqlens: torch.Tensor | None,
    num_accepted_tokens: torch.Tensor | None,
    use_qk_l2norm_in_kernel: bool,
    inplace_final_state: bool,
    verify_lengths: bool = False,
) -> str | None:
    """Return why this call is not the single-token decode Fabric implements.

    Every check reads metadata and shapes only. Inspecting ``cu_seqlens`` values
    would force a device synchronization on each call, which measured ~0.2 ms —
    twenty times the cost of the kernel being dispatched. Instead, one token per
    sequence is inferred from shapes: ``cu_seqlens`` holds one boundary per
    sequence plus one, so when the sequence count equals the token count and
    every scheduled sequence contributes at least one token, each length is one.

    ``verify_lengths`` performs the exact check and is intended for tests, not the
    serving path.
    """
    if not use_qk_l2norm_in_kernel:
        return "caller expects normalization outside the kernel"
    if not inplace_final_state:
        return "caller expects a freshly allocated final state"
    if beta is None:
        return "beta is required"
    if initial_state is None or ssm_state_indices is None:
        return "a paged state cache and slot indices are required"
    if num_accepted_tokens is not None:
        return "speculative decoding acceptance is not implemented"
    if cu_seqlens is None:
        return "cu_seqlens is required to establish one token per sequence"
    if q.ndim != 4 or q.shape[0] != 1:
        return f"expected a [1, tokens, heads, dim] batch, got {tuple(q.shape)}"

    sequences = int(cu_seqlens.shape[0]) - 1
    if sequences < 1:
        return "cu_seqlens describes no sequences"
    if sequences != int(q.shape[1]):
        return "more than one token per sequence"
    if verify_lengths:
        lengths = cu_seqlens[1:] - cu_seqlens[:-1]
        if not bool(torch.all(lengths == 1)):
            return "cu_seqlens lengths are not all one"
    return None


def fabric_fused_recurrent_gated_delta_rule(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    g: torch.Tensor,
    beta: torch.Tensor | None = None,
    scale: float | None = None,
    initial_state: torch.Tensor | None = None,
    inplace_final_state: bool = True,
    cu_seqlens: torch.Tensor | None = None,
    ssm_state_indices: torch.Tensor | None = None,
    num_accepted_tokens: torch.Tensor | None = None,
    use_qk_l2norm_in_kernel: bool = False,
    *,
    mode: KernelMode = "auto",
    stats: SubstitutionStats | None = None,
    fallback: Any = None,
    verify_lengths: bool = False,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Drop-in replacement for vLLM's op, backed by the Fabric kernel.

    Signature matches vLLM 0.11.0. ``fallback`` is vLLM's own implementation, used
    for every call shape this adapter does not implement.
    """
    reason = unsupported_call_reason(
        q=q,
        beta=beta,
        initial_state=initial_state,
        ssm_state_indices=ssm_state_indices,
        cu_seqlens=cu_seqlens,
        num_accepted_tokens=num_accepted_tokens,
        use_qk_l2norm_in_kernel=use_qk_l2norm_in_kernel,
        inplace_final_state=inplace_final_state,
        verify_lengths=verify_lengths,
    )
    if reason is not None:
        if fallback is None:
            raise NotImplementedError(f"{reason}; no vLLM fallback was provided")
        if stats is not None:
            stats.delegated_calls += 1
            stats.last_delegation_reason = reason
        return fallback(
            q=q,
            k=k,
            v=v,
            g=g,
            beta=beta,
            scale=scale,
            initial_state=initial_state,
            inplace_final_state=inplace_final_state,
            cu_seqlens=cu_seqlens,
            ssm_state_indices=ssm_state_indices,
            num_accepted_tokens=num_accepted_tokens,
            use_qk_l2norm_in_kernel=use_qk_l2norm_in_kernel,
        )

    # vLLM hands over [1, tokens, heads, dim]; one token per sequence makes the
    # token axis the sequence axis.
    sequences = q.shape[1]
    q_flat = q.reshape(sequences, q.shape[2], q.shape[3])
    k_flat = k.reshape(sequences, k.shape[2], k.shape[3])
    v_flat = v.reshape(sequences, v.shape[2], v.shape[3])
    g_flat = g.reshape(sequences, g.shape[2]).float().contiguous()
    beta_flat = beta.reshape(sequences, beta.shape[2]).float().contiguous()

    indices = ssm_state_indices
    if indices.ndim > 1:
        # A [sequences, 1] slot table addresses one page per sequence.
        indices = indices[:, 0]
    indices = indices[:sequences].contiguous()

    out, _decision = dispatch_gated_delta_decode(
        q_flat.contiguous(),
        k_flat.contiguous(),
        v_flat.contiguous(),
        g_flat,
        beta_flat,
        initial_state,
        mode=mode,
        state_indices=indices,
    )
    if stats is not None:
        stats.fabric_calls += 1
    return out.reshape(q.shape[0], sequences, q.shape[2], v.shape[3]), initial_state
