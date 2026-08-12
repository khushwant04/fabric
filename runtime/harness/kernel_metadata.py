"""Compiled-kernel metadata capture.

Registers, spills, shared memory, and launch geometry are part of the kernel
metric set, and they are only available from Triton's compiled-kernel objects.
That surface is a Triton internal, so every read is guarded: when the layout
changes, capture returns ``None`` and the artifact records the absence instead of
failing a benchmark run.

Metadata is captured by snapshotting the compilation cache around the *first*
launch of a configuration, which avoids parsing Triton's cache keys.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

METADATA_FIELDS = ("shared", "num_warps", "num_stages", "name")


def _compiled_entries(kernel: Any) -> dict[Any, Any]:
    """Flatten Triton's per-device compilation caches into one mapping."""
    caches = getattr(kernel, "device_caches", None)
    if not caches:
        return {}
    flattened: dict[Any, Any] = {}
    try:
        for device, entry in caches.items():
            # Triton 3.4 stores (compiled_kernels, target, backend, binder).
            compiled = entry[0] if isinstance(entry, tuple | list) else entry
            if isinstance(compiled, dict):
                for key, value in compiled.items():
                    flattened[(device, key)] = value
    except (AttributeError, IndexError, TypeError):  # pragma: no cover - defensive
        return {}
    return flattened


def describe(compiled: Any) -> dict[str, Any]:
    """Extract the recorded metric fields from one compiled kernel."""
    described: dict[str, Any] = {
        "registers": getattr(compiled, "n_regs", None),
        "spills": getattr(compiled, "n_spills", None),
    }
    metadata = getattr(compiled, "metadata", None)
    for field in METADATA_FIELDS:
        value = getattr(metadata, field, None) if metadata is not None else None
        described["shared_memory_bytes" if field == "shared" else field] = value
    return described


def capture(kernel: Any, launch: Callable[[], Any]) -> dict[str, Any] | None:
    """Run ``launch`` and return metadata for the kernel it compiles.

    Returns ``None`` when the configuration was already compiled in this process
    or when Triton's cache layout is not recognized, because in neither case can
    the new entry be attributed to this launch.
    """
    before = set(_compiled_entries(kernel))
    launch()
    after = _compiled_entries(kernel)
    added = [key for key in after if key not in before]
    if len(added) != 1:
        return None
    return describe(after[added[0]])
