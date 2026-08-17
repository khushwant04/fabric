"""Where vLLM keeps its gated-delta ops, which moves between versions.

The comparison and the adapter both need to find vLLM's own kernel. The version this
project pinned first exposes it at ``model_executor.layers.fla.ops``; the version that
can serve the launch model moved it to ``third_party.flash_linear_attention.ops``.
Hard-coding either path means the comparison silently stops being runnable when the
version changes, which is how the substitution ended up unregistered in the live host
without that being obvious.

Resolving it at import time keeps one implementation working across both, and makes the
location an observable fact: the artifact records which module the baseline came from,
so a reader can tell which vLLM the numbers describe.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

#: Searched in order. The newest layout first, so a current install is not matched by a
#: compatibility shim an older path might still provide.
_CANDIDATE_MODULES = (
    "vllm.third_party.flash_linear_attention.ops.fused_recurrent",
    "vllm.model_executor.layers.fla.ops.fused_recurrent",
    "vllm.model_executor.layers.mamba.gdn.fused_recurrent",
)

#: The unfused decode op: recurrence only, with gating and normalisation already done by
#: the caller. This is the operation the Fabric kernel replaces.
UNFUSED = "fused_recurrent_gated_delta_rule"

#: The fused decode op newer versions add. It also unpacks a combined QKV tensor,
#: L2-normalises q and k, and computes the gate from A_log/dt_bias, so it does strictly
#: more work per call than the unfused op.
PACKED = "fused_recurrent_gated_delta_rule_packed_decode"


@dataclass(frozen=True)
class VllmOps:
    """vLLM's gated-delta entry points, as found in this environment."""

    module: str
    version: str | None
    unfused: Any
    packed: Any | None

    @property
    def has_packed_decode(self) -> bool:
        return self.packed is not None


def _vllm_version() -> str | None:
    try:
        import vllm
    except ImportError:
        return None
    return getattr(vllm, "__version__", None)


def resolve() -> VllmOps | None:
    """Find vLLM's gated-delta ops, or ``None`` when vLLM is not installed.

    Returning ``None`` rather than raising keeps the harness usable in an environment
    without vLLM, where it records the baseline as unavailable with a reason.
    """
    for name in _CANDIDATE_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        unfused = getattr(module, UNFUSED, None)
        if unfused is None:
            continue
        return VllmOps(
            module=name,
            version=_vllm_version(),
            unfused=unfused,
            # Looked up on the ops package, not this module: newer versions export it
            # from the package while defining it elsewhere.
            packed=_packed_from_package(name),
        )
    return None


def _packed_from_package(module_name: str) -> Any | None:
    package = module_name.rsplit(".", 1)[0]
    try:
        ops = importlib.import_module(package)
    except ImportError:
        return None
    return getattr(ops, PACKED, None)
