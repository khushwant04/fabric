"""Register the Fabric kernel as the gated-delta decode op a live vLLM calls.

Two facts about vLLM decide how this has to work.

The layer does ``from ...ops import fused_recurrent_gated_delta_rule_packed_decode`` at
import time and then calls that name directly. Replacing the attribute on the module that
defines the op therefore changes nothing: the caller already holds its own reference. The
name has to be replaced in the calling module too.

The engine runs in its own process. A patch applied in the process that starts the server
is not present in the process that runs the model, so patching once at startup would
appear to work and then serve vLLM's kernel anyway. Everything here is arranged to happen
in whichever process imports the layer, which is why it is driven from ``sitecustomize``:
that is imported by every interpreter, including spawned children.

Substitution is off unless ``FABRIC_KERNEL`` is set, so one image can serve both sides of a
comparison and the only difference between them is that variable. Building two images would
leave every other difference between them as a possible explanation for a difference in
speed.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import os
import sys
from typing import Any

#: The module that defines vLLM's op, and the one that imported the name and calls it.
_OPS_MODULES = (
    "vllm.third_party.flash_linear_attention.ops.fused_recurrent",
    "vllm.third_party.flash_linear_attention.ops",
)
_CALLER_MODULES = ("vllm.model_executor.layers.mamba.gdn.qwen_gdn_linear_attn",)

_OP_NAME = "fused_recurrent_gated_delta_rule_packed_decode"

#: The tile is chosen per GPU and batch by ``kernels.launch_config``: explicit arguments,
#: then ``FABRIC_KERNEL_BLOCK_V`` / ``FABRIC_KERNEL_NUM_WARPS`` / ``FABRIC_KERNEL_NUM_STAGES``,
#: then the file named by ``FABRIC_KERNEL_TUNING_FILE``, then the per-compute-capability
#: table, then vLLM's own tiling (32 wide, one warp) for a GPU the table does not know.
#:
#: Tiles have to be measured the way the host runs them. A 16-wide tile measured faster in
#: isolation and slower in service on the development GPU: vLLM captures decode into CUDA
#: graphs, and inside a graph there is no launch to overlap, so a shape that won by hiding
#: launch overhead has nothing left to win with. The T4 entries in the table were measured
#: in graph replay; entries marked unmeasured are reasoned, and a sweep result dropped in
#: as a tuning file replaces them without a new image. Setting the three variables above
#: to 32, 1 and 3 restores the exact like-for-like tile on every GPU.

#: Calls handed back to vLLM's own op because the Fabric kernel must not serve them.
delegations: dict[str, int] = {}


def _delegate_reason(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """Why this call must go to vLLM's op instead, or ``None``. Host-side only."""
    from kernels.launch_config import bf16_unsupported_reason, device_capability

    mixed_qkv = args[0] if args else kwargs.get("mixed_qkv")
    if mixed_qkv is None or not getattr(mixed_qkv, "is_cuda", False):
        return None
    return bf16_unsupported_reason(mixed_qkv.dtype, device_capability(mixed_qkv.device))


def _fabric_packed_decode(*args: Any, **kwargs: Any):
    from kernels.gated_delta_packed_decode import fused_packed_decode

    reason = _delegate_reason(args, kwargs)
    original = getattr(_fabric_packed_decode, "_fabric_original", None)
    if reason is not None and original is not None:
        if not delegations:
            print(f"[fabric] delegating to vLLM's op: {reason}", file=sys.stderr, flush=True)
        delegations[reason] = delegations.get(reason, 0) + 1
        return original(*args, **kwargs)
    return fused_packed_decode(*args, **kwargs)


def _patch_module(module: Any) -> bool:
    """Replace the op on an already-imported module, if it has one."""
    if not hasattr(module, _OP_NAME):
        return False
    original = getattr(module, _OP_NAME)
    if getattr(original, "_fabric_substitution", False):
        return False
    _fabric_packed_decode._fabric_substitution = True  # type: ignore[attr-defined]
    # Kept so the substitution can be reversed and so the original stays reachable for a
    # comparison running in the same process.
    _fabric_packed_decode._fabric_original = original  # type: ignore[attr-defined]
    setattr(module, _OP_NAME, _fabric_packed_decode)
    return True


class _PatchOnImport(importlib.abc.MetaPathFinder):
    """Patch a module immediately after it is first executed.

    The layer has to be patched after it exists and before it runs, in whichever process
    imports it. A finder that defers to the normal machinery and wraps the loader is the
    narrowest way to get that: nothing is imported early, and no module is imported that
    would not have been imported anyway.
    """

    def __init__(self, targets: tuple[str, ...]) -> None:
        self._targets = set(targets)
        self._in_progress: set[str] = set()

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001, ANN201
        if fullname not in self._targets or fullname in self._in_progress:
            return None
        self._in_progress.add(fullname)
        try:
            # Resolved through the remaining finders, so the real module is loaded
            # normally and this only observes it.
            spec = importlib.util.find_spec(fullname)
        except (ImportError, AttributeError, ValueError):
            return None
        finally:
            self._in_progress.discard(fullname)

        if spec is None or spec.loader is None:
            return None

        loader = spec.loader
        original_exec = loader.exec_module

        def exec_module(module):  # noqa: ANN001, ANN202
            original_exec(module)
            _patch_module(module)

        loader.exec_module = exec_module  # type: ignore[method-assign]
        return spec


def enabled() -> bool:
    """Whether substitution was asked for."""
    return os.environ.get("FABRIC_KERNEL", "").strip().lower() in {"1", "true", "yes", "on"}


def install() -> dict[str, Any]:
    """Arrange for the Fabric kernel to be used, and report what was done.

    Modules already imported are patched now; the rest are patched as they are imported.
    Both are needed: the caller is usually imported later, but a process that has already
    loaded it would otherwise keep vLLM's op.
    """
    from kernels import launch_config

    # Read and validate the tuning file now, in the process that will decode: a bad file
    # raises here, which sitecustomize reports loudly and which leaves vLLM's op in place,
    # rather than failing the first request.
    tile = launch_config.reload()

    patched_now = []
    for name in (*_OPS_MODULES, *_CALLER_MODULES):
        module = sys.modules.get(name)
        if module is not None and _patch_module(module):
            patched_now.append(name)

    finder = _PatchOnImport((*_OPS_MODULES, *_CALLER_MODULES))
    # Ahead of the standard finders, so the wrap is in place before the module executes.
    sys.meta_path.insert(0, finder)

    return {
        "patched_immediately": patched_now,
        "deferred": [*_OPS_MODULES, *_CALLER_MODULES],
        "tile": tile,
    }
