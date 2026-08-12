"""Kernel dispatch and fallback for a model host.

A serving host must decide, per call, whether to run the Fabric Triton kernel or
the authoritative eager path, and it must never fail a request because a custom
kernel could not run. This module is that decision point.

The three modes mirror `runtime.kernel_mode` in the control plane's deployment
spec, so a deployment's declared intent maps directly onto runtime behaviour:

* ``auto`` — prefer the Fabric kernel, fall back to eager on any unsupported
  input or kernel failure. This is what production deployments use.
* ``fabric`` — require the Fabric kernel and raise if it cannot run. Benchmarks
  and release gates use this, because a silent fallback would otherwise be
  measured and reported as Fabric performance.
* ``standard`` — always use the eager path, giving an operator a way to take a
  suspect kernel out of service without redeploying a different image.

Fallbacks are counted and the last reason retained, so a host can expose them as
telemetry instead of degrading invisibly.
"""

from __future__ import annotations

import dataclasses
import pathlib
import sys
import threading
from typing import Any, Literal

import torch

RUNTIME_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from kernels.gated_delta_decode import (  # noqa: E402
    gated_delta_decode,
    torch_gated_delta_decode_reference,
)

KernelMode = Literal["auto", "fabric", "standard"]
SUPPORTED_MODES: tuple[KernelMode, ...] = ("auto", "fabric", "standard")

PATH_FABRIC = "fabric"
PATH_EAGER = "eager"

#: Dtypes the fused kernel accepts for Q, K, and V.
SUPPORTED_DTYPES = (torch.float16, torch.bfloat16, torch.float32)

#: The kernel indexes the key dimension with a single power-of-two tile.
MAX_KEY_DIM = 256


class KernelUnavailableError(RuntimeError):
    """Raised in ``fabric`` mode when the Fabric kernel cannot serve a call."""


@dataclasses.dataclass(frozen=True)
class DispatchDecision:
    """Which path served a call, and why."""

    mode: KernelMode
    path: str
    reason: str

    @property
    def used_fabric(self) -> bool:
        return self.path == PATH_FABRIC


@dataclasses.dataclass
class DispatchStats:
    """Counters a host can publish as telemetry.

    A silent fallback is an operational event: the deployment is no longer
    running the kernel it was measured with, so it has to be observable.
    """

    fabric_calls: int = 0
    eager_calls: int = 0
    fallback_calls: int = 0
    last_fallback_reason: str | None = None

    def record(self, decision: DispatchDecision, *, fell_back: bool) -> None:
        if decision.used_fabric:
            self.fabric_calls += 1
        else:
            self.eager_calls += 1
        if fell_back:
            self.fallback_calls += 1
            self.last_fallback_reason = decision.reason

    def snapshot(self) -> dict[str, Any]:
        total = self.fabric_calls + self.eager_calls
        return {
            "fabric_calls": self.fabric_calls,
            "eager_calls": self.eager_calls,
            "fallback_calls": self.fallback_calls,
            "fabric_fraction": self.fabric_calls / total if total else 0.0,
            "last_fallback_reason": self.last_fallback_reason,
        }


def unsupported_reason(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    g: torch.Tensor,
    beta: torch.Tensor,
    state: torch.Tensor,
) -> str | None:
    """Return why the Fabric kernel cannot serve these inputs, or ``None``.

    Only cheap, host-visible preconditions are checked here. The kernel performs
    full validation itself; this exists so the common cases are rejected without
    entering a launch that would raise.
    """
    if not q.is_cuda:
        return "inputs are not on a CUDA device"
    if q.dtype not in SUPPORTED_DTYPES:
        return f"unsupported dtype {q.dtype}"
    if not (q.dtype == k.dtype == v.dtype):
        return "q, k, and v have mixed dtypes"
    if state.dtype is not torch.float32:
        return "recurrent state is not FP32"
    if q.ndim != 3 or q.shape[-1] > MAX_KEY_DIM:
        return f"key dimension exceeds {MAX_KEY_DIM} or rank is not 3"
    if any(not tensor.is_contiguous() for tensor in (q, k, v, g, beta, state)):
        return "inputs are not contiguous"
    return None


def dispatch_gated_delta_decode(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    g: torch.Tensor,
    beta: torch.Tensor,
    state: torch.Tensor,
    *,
    mode: KernelMode = "auto",
    out: torch.Tensor | None = None,
    block_v: int = 32,
    stats: DispatchStats | None = None,
) -> tuple[torch.Tensor, DispatchDecision]:
    """Run one decode step through the mode's chosen path.

    Returns the output and the decision that produced it. ``state`` is updated in
    place by both paths, so a fallback mid-generation keeps the recurrence valid.
    """
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"unknown kernel mode {mode!r}; expected one of {SUPPORTED_MODES}")

    if mode == "standard":
        decision = DispatchDecision(mode, PATH_EAGER, "standard mode requested")
        result = torch_gated_delta_decode_reference(q, k, v, g, beta, state, out=out)
        if stats is not None:
            stats.record(decision, fell_back=False)
        return result, decision

    reason = unsupported_reason(q, k, v, g, beta, state)
    if reason is None:
        try:
            result = gated_delta_decode(q, k, v, g, beta, state, out=out, block_v=block_v)
        except Exception as exc:  # noqa: BLE001 - a kernel failure must not fail a request
            reason = f"{type(exc).__name__}: {exc}"
        else:
            decision = DispatchDecision(mode, PATH_FABRIC, "fabric kernel selected")
            if stats is not None:
                stats.record(decision, fell_back=False)
            return result, decision

    if mode == "fabric":
        # Strict mode never silently degrades: a benchmark must not attribute
        # eager performance to the Fabric kernel.
        raise KernelUnavailableError(reason)

    decision = DispatchDecision(mode, PATH_EAGER, reason)
    result = torch_gated_delta_decode_reference(q, k, v, g, beta, state, out=out)
    if stats is not None:
        stats.record(decision, fell_back=True)
    return result, decision


class GatedDeltaDecoder:
    """Thread-safe dispatcher holding one deployment's mode and counters.

    A host creates one per deployment so its `kernel_mode` and fallback
    telemetry stay attributable to that deployment.
    """

    def __init__(self, mode: KernelMode = "auto", *, block_v: int = 32) -> None:
        if mode not in SUPPORTED_MODES:
            raise ValueError(f"unknown kernel mode {mode!r}; expected one of {SUPPORTED_MODES}")
        self.mode = mode
        self.block_v = block_v
        self.stats = DispatchStats()
        self._lock = threading.Lock()

    def __call__(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        g: torch.Tensor,
        beta: torch.Tensor,
        state: torch.Tensor,
        *,
        out: torch.Tensor | None = None,
    ) -> torch.Tensor:
        result, decision = dispatch_gated_delta_decode(
            q,
            k,
            v,
            g,
            beta,
            state,
            mode=self.mode,
            out=out,
            block_v=self.block_v,
            stats=None,
        )
        with self._lock:
            self.stats.record(decision, fell_back=not decision.used_fabric and self.mode == "auto")
        return result

    def telemetry(self) -> dict[str, Any]:
        with self._lock:
            return {"kernel_mode": self.mode, "block_v": self.block_v, **self.stats.snapshot()}
