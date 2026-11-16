"""Per-architecture launch configuration for the gated-delta decode kernels.

The best tile is a property of the GPU and the batch, not of the algorithm. A T4 has 40
SMs and about 320 GB/s; an A10 has 72 SMs and about 600 GB/s. At batch 1 with sixteen value
heads and V=128, a 32-wide value tile launches 64 programs: fewer than an A10 has SMs and
1.6 waves on a T4. A 16-wide tile launches 128. At larger batches the step is bound by the
~2 MB of recurrent state each token reads and writes, the grid is many waves deep on
either GPU, and the tile matters much less. So the choice is keyed by compute capability and
by batch bucket.

Every table entry says whether it was measured. ``measured=True`` entries cite the
committed artifact that recorded them; the rest carry the reasoning that chose them and
must be replaced by a sweep (``harness.tune``) before anyone calls them tuned.

Resolution order, per field, for a call that does not pass a tile:

1. explicit ``block_v`` / ``num_warps`` / ``num_stages`` arguments,
2. ``FABRIC_KERNEL_BLOCK_V`` / ``FABRIC_KERNEL_NUM_WARPS`` / ``FABRIC_KERNEL_NUM_STAGES``
   (packed kernel only, as before: these were the serving override knobs),
3. an entry in the JSON file named by ``FABRIC_KERNEL_TUNING_FILE``,
4. this module's table for the device's compute capability,
5. the conservative default (the host's own tiling).

A tile is coupled: ``num_warps`` chosen for a 16-wide tile is wrong for a 128-wide one. So
when ``block_v`` comes from an explicit argument or the environment and ``num_warps`` does
not, ``num_warps`` is derived from ``block_v`` by the kernel's own rule rather than taken
from a table entry tuned for a different tile. ``num_stages`` is independent of the tile.

Lookups are host-only and cached per device and batch: nothing here touches device memory
or synchronizes, so it is safe inside a CUDA-graph-captured decode step.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
import pathlib
import re
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import torch

# torch is imported where it is used, not here: a serving host validates its tuning file at
# interpreter startup (sitecustomize), and every interpreter should not pay for torch there.

TUNING_FILE_ENV = "FABRIC_KERNEL_TUNING_FILE"
TUNING_SCHEMA = "fabric-kernel-tuning/v1"

#: Environment overrides, applied to the packed kernel only (they predate this module and
#: were always the serving host's knobs).
ENV_BLOCK_V = "FABRIC_KERNEL_BLOCK_V"
ENV_NUM_WARPS = "FABRIC_KERNEL_NUM_WARPS"
ENV_NUM_STAGES = "FABRIC_KERNEL_NUM_STAGES"

KERNEL_PACKED = "packed"
KERNEL_RECURRENT = "recurrent"
KERNELS = (KERNEL_PACKED, KERNEL_RECURRENT)

#: Inclusive batch ranges. ``None`` is unbounded.
BATCH_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("1", 1, 1),
    ("2-4", 2, 4),
    ("5-16", 5, 16),
    ("17+", 17, None),
)
BUCKET_LABELS = tuple(label for label, _, _ in BATCH_BUCKETS)

#: Below this, bfloat16 does not exist in hardware (Ampere introduced it).
BF16_MIN_CAPABILITY = (8, 0)

_VALID_WARPS = (1, 2, 4, 8)
_VALID_BLOCK_V = {
    KERNEL_PACKED: (8, 16, 32, 64, 128, 256),
    KERNEL_RECURRENT: (8, 16, 32),
}
_STAGES_RANGE = (1, 6)

_T4_ARTIFACTS = (
    "runtime/artifacts/t4-production/20261006-20261006T132704.010336Z-nogit.json",
    "runtime/artifacts/t4-production/20261006-20261006T132727.099344Z-nogit.json",
)
_RTX4070_ARTIFACTS = (
    "runtime/artifacts/rtx4070-dev/20260817T113322.370847Z-2ca24bfe1479.json",
    "runtime/artifacts/rtx4070-dev/20260817T113636.424894Z-2ca24bfe1479.json",
    "runtime/artifacts/rtx4070-dev/20260817T113951.681459Z-2ca24bfe1479.json",
    "runtime/artifacts/rtx4070-dev/20260817T115019.647728Z-5cba0a35911f.json",
    "runtime/artifacts/rtx4070-dev/20260817T115308.230285Z-5cba0a35911f.json",
)


class TuningFileError(ValueError):
    """The tuning file named by ``FABRIC_KERNEL_TUNING_FILE`` cannot be used."""


@dataclasses.dataclass(frozen=True)
class LaunchConfig:
    """One kernel launch shape and where it came from."""

    block_v: int
    num_warps: int
    num_stages: int
    measured: bool = False
    source: str = "default"
    note: str = ""
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "block_v": self.block_v,
            "num_warps": self.num_warps,
            "num_stages": self.num_stages,
            "measured": self.measured,
            "source": self.source,
            "note": self.note,
            "evidence": list(self.evidence),
        }


def _entry(
    block_v: int,
    num_warps: int,
    num_stages: int,
    *,
    measured: bool,
    note: str,
    evidence: tuple[str, ...] = (),
) -> LaunchConfig:
    return LaunchConfig(block_v, num_warps, num_stages, measured, "table", note, evidence)


def legacy_num_warps(kernel: str, block_v: int) -> int:
    """The warp count each kernel derived from its tile before this table existed."""
    if kernel == KERNEL_PACKED:
        return max(1, block_v // 32)
    return 4 if block_v <= 16 else 8


#: Used for any compute capability without a table entry. These are the tilings the
#: kernels used before this module: vLLM's own for the packed kernel (so an untuned call is
#: a like-for-like swap), and the recurrence kernel's historical 32/8.
DEFAULTS: dict[str, LaunchConfig] = {
    KERNEL_PACKED: LaunchConfig(
        32, 1, 3, False, "default", "vLLM's own tiling: like-for-like with the host's kernel"
    ),
    KERNEL_RECURRENT: LaunchConfig(32, 8, 3, False, "default", "historical kernel default"),
}

_T4_PACKED_MEASURED = (
    "T4 sweep in CUDA-graph replay, FP16, H16/HV16 and H16/HV32 at K=V=128: fastest "
    "candidate that passed the exact (atol=0) gate against stock vLLM was 16/1 at batches "
    "1, 4, 8 and 16 (HV16) and 1, 4, 8 (HV32). Recorded at num_stages=3, the comparator's "
    "fixed value; this kernel has no loop for Triton's pipeliner to stage and SM75 has no "
    "cp.async, so stages are not expected to change it, but 1 and 2 are unmeasured. Both "
    "sweep artifacts have status 'fail' because multi-warp candidates missed the exact gate; "
    "the selected one-warp candidates passed individually."
)

#: Keyed by kernel, then "major.minor", then batch bucket.
TABLE: dict[str, dict[str, dict[str, LaunchConfig]]] = {
    KERNEL_PACKED: {
        # Tesla T4 (Turing): 40 SMs, ~320 GB/s, 64 KB shared memory per SM, no bf16, no
        # cp.async (so Triton's num_stages pipelining has nothing to drive).
        "7.5": {
            "1": _entry(16, 1, 3, measured=True, note=_T4_PACKED_MEASURED, evidence=_T4_ARTIFACTS),
            "2-4": _entry(
                16, 1, 3, measured=True, note=_T4_PACKED_MEASURED, evidence=_T4_ARTIFACTS
            ),
            "5-16": _entry(
                16,
                1,
                3,
                measured=True,
                note=_T4_PACKED_MEASURED
                + " At batch 16 with HV32 the fastest passing tile was 32/1; "
                "16/1 won at every other measured point in this bucket.",
                evidence=_T4_ARTIFACTS,
            ),
            "17+": _entry(
                32,
                1,
                1,
                measured=False,
                note="Unmeasured. Bandwidth bound: the grid is many waves deep on 40 SMs, so "
                "a wider tile costs no parallelism and halves q/k re-reads and gate "
                "recomputation; the nearest measured point (HV32 batch 16, the same program "
                "count as HV16 batch 32) chose 32/1. One warp because every multi-warp "
                "candidate missed the exact gate on T4. num_stages=1: no loop and no "
                "cp.async on SM75, so there is nothing to pipeline.",
            ),
        },
        # NVIDIA A10 (Ampere, GA102): 72 SMs, ~600 GB/s, bf16 supported. Nothing measured.
        "8.6": {
            "1": _entry(
                16,
                1,
                3,
                measured=False,
                note="Unmeasured. A 32-wide tile at batch 1 launches 64 programs (HV16, "
                "V128), fewer than 72 SMs; 16 launches 128 and occupies every SM. The same "
                "choice was measured fastest at batch 1 on T4.",
            ),
            "2-4": _entry(
                32,
                1,
                3,
                measured=False,
                note="Unmeasured. 32-wide already launches >=128 programs at batch 2, so "
                "keep vLLM's tile and avoid re-reading q/k per extra tile.",
            ),
            "5-16": _entry(
                32,
                1,
                3,
                measured=False,
                note="Unmeasured. vLLM's tile; the grid is several waves deep.",
            ),
            "17+": _entry(
                32,
                1,
                3,
                measured=False,
                note="Unmeasured. A wider tile (64/128) would cut q/k re-reads further but "
                "needs more than one warp to hold the [BV, K] FP32 state without spilling, "
                "and multi-warp tiles missed the exact gate against stock vLLM on T4. Kept "
                "at vLLM's tile until an A10 sweep shows a passing wider tile is faster.",
            ),
        },
        # RTX 4070 Laptop (development). No packed-kernel artifact exists for it.
        "8.9": {
            label: _entry(
                32,
                1,
                3,
                measured=False,
                note="Unmeasured for the packed kernel: vLLM's tile. (A 16-wide tile was "
                "observed faster in isolation and slower inside CUDA graphs during "
                "development, but no artifact records that.)",
            )
            for label in BUCKET_LABELS
        },
    },
    KERNEL_RECURRENT: {
        "7.5": {
            "1": _entry(
                16,
                4,
                1,
                measured=False,
                note="Unmeasured. 128 programs instead of 64 on 40 SMs (3.2 waves rather "
                "than 1.6); the packed kernel measured the same preference on T4. "
                "num_stages=1: no loop and no cp.async on SM75.",
            ),
            "2-4": _entry(
                16,
                4,
                1,
                measured=False,
                note="Unmeasured. Mirrors the packed kernel's measured T4 preference.",
            ),
            "5-16": _entry(
                32,
                8,
                1,
                measured=False,
                note="Unmeasured. Bandwidth bound; historical tile.",
            ),
            "17+": _entry(
                32, 8, 1, measured=False, note="Unmeasured. Bandwidth bound; historical tile."
            ),
        },
        "8.6": {
            "1": _entry(
                16,
                4,
                3,
                measured=False,
                note="Unmeasured. 64 programs at 32-wide is fewer than 72 SMs.",
            ),
            "2-4": _entry(32, 8, 3, measured=False, note="Unmeasured. Historical tile."),
            "5-16": _entry(32, 8, 3, measured=False, note="Unmeasured. Historical tile."),
            "17+": _entry(32, 8, 3, measured=False, note="Unmeasured. Historical tile."),
        },
        "8.9": {
            "1": _entry(
                32,
                8,
                3,
                measured=True,
                note="RTX 4070 Laptop value-tile sweep (8/16/32), FP16, 16 heads, K=V=128, "
                "eager launches: 32 fastest at batch 1 in four of five runs.",
                evidence=_RTX4070_ARTIFACTS,
            ),
            "2-4": _entry(
                32,
                8,
                3,
                measured=True,
                note="RTX 4070 Laptop sweep: 32 fastest at batches 2 and 4 in all five runs.",
                evidence=_RTX4070_ARTIFACTS,
            ),
            "5-16": _entry(
                32,
                8,
                3,
                measured=True,
                note="RTX 4070 Laptop sweep: 32 fastest at batch 8 in all five runs (16 "
                "not measured).",
                evidence=_RTX4070_ARTIFACTS,
            ),
            "17+": _entry(
                32, 8, 3, measured=False, note="Unmeasured above batch 8; historical tile."
            ),
        },
    },
}


def batch_bucket(batch: int) -> str:
    """Return the bucket label for a batch size."""
    if batch < 1:
        raise ValueError("batch must be positive")
    for label, low, high in BATCH_BUCKETS:
        if batch >= low and (high is None or batch <= high):
            return label
    raise AssertionError("unreachable: buckets cover every positive batch")


def capability_key(capability: tuple[int, int]) -> str:
    return f"{capability[0]}.{capability[1]}"


@functools.cache
def _capability_for_index(index: int) -> tuple[int, int]:
    import torch

    return tuple(torch.cuda.get_device_capability(index))  # type: ignore[return-value]


def device_capability(device: torch.device | str | int | None = None) -> tuple[int, int]:
    """Compute capability of a CUDA device, cached per device index.

    Host-side only: the driver query does not touch a stream, and caching keeps it off the
    decode path after the first call.
    """
    import torch

    if isinstance(device, int):
        index = device
    else:
        device = torch.device("cuda") if device is None else torch.device(device)
        if device.type != "cuda":
            raise ValueError(f"{device} is not a CUDA device")
        index = device.index if device.index is not None else torch.cuda.current_device()
    return _capability_for_index(index)


def bf16_unsupported_reason(dtype: torch.dtype, capability: tuple[int, int]) -> str | None:
    """Why ``dtype`` must not reach a Fabric kernel on this capability, or ``None``."""
    import torch

    if dtype is torch.bfloat16 and tuple(capability) < BF16_MIN_CAPABILITY:
        return (
            f"bfloat16 inputs on compute capability {capability_key(capability)}: SM < 8.0 "
            "has no native bfloat16, so the Fabric kernel is not used; serve FP16 "
            "(--dtype half) on this GPU"
        )
    return None


# --- tuning file -------------------------------------------------------------------------


def _check_tile(kernel: str, block_v: Any, num_warps: Any, num_stages: Any) -> str | None:
    def is_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    if not is_int(block_v) or block_v not in _VALID_BLOCK_V[kernel]:
        return f"block_v must be one of {_VALID_BLOCK_V[kernel]} for the {kernel} kernel"
    if not is_int(num_warps) or num_warps not in _VALID_WARPS:
        return f"num_warps must be one of {_VALID_WARPS}"
    low, high = _STAGES_RANGE
    if not is_int(num_stages) or not low <= num_stages <= high:
        return f"num_stages must be an integer from {low} to {high}"
    return None


def parse_tuning(
    payload: Any, *, origin: str = "<tuning>"
) -> dict[tuple[str, str, str], LaunchConfig]:
    """Validate a tuning document and return its entries keyed by (kernel, cc, bucket).

    Rejected as a whole on any error: a file that is half applied would serve a
    configuration nobody chose.
    """

    def fail(message: str) -> TuningFileError:
        return TuningFileError(f"{origin}: {message}")

    if not isinstance(payload, dict):
        raise fail("tuning file must be a JSON object")
    if payload.get("schema") != TUNING_SCHEMA:
        raise fail(f"schema must be {TUNING_SCHEMA!r}, got {payload.get('schema')!r}")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise fail("entries must be a non-empty list")
    parsed: dict[tuple[str, str, str], LaunchConfig] = {}
    for position, entry in enumerate(entries):
        where = f"entries[{position}]"
        if not isinstance(entry, dict):
            raise fail(f"{where} must be an object")
        kernel = entry.get("kernel")
        if kernel not in KERNELS:
            raise fail(f"{where}.kernel must be one of {KERNELS}, got {kernel!r}")
        cc = entry.get("compute_capability")
        if not isinstance(cc, str) or not re.fullmatch(r"\d+\.\d+", cc):
            raise fail(f"{where}.compute_capability must be a string like '7.5', got {cc!r}")
        bucket = entry.get("batch_bucket")
        if bucket not in BUCKET_LABELS:
            raise fail(f"{where}.batch_bucket must be one of {BUCKET_LABELS}, got {bucket!r}")
        problem = _check_tile(
            kernel, entry.get("block_v"), entry.get("num_warps"), entry.get("num_stages")
        )
        if problem:
            raise fail(f"{where}: {problem}")
        measured = entry.get("measured")
        if not isinstance(measured, bool):
            raise fail(f"{where}.measured must be true or false")
        note = entry.get("note", "")
        if not isinstance(note, str):
            raise fail(f"{where}.note must be a string")
        key = (kernel, cc, bucket)
        if key in parsed:
            raise fail(f"{where} duplicates an earlier entry for {key}")
        parsed[key] = LaunchConfig(
            entry["block_v"],
            entry["num_warps"],
            entry["num_stages"],
            measured,
            f"tuning-file:{origin}",
            note,
        )
    return parsed


def load_tuning_file(path: str | os.PathLike[str]) -> dict[tuple[str, str, str], LaunchConfig]:
    """Read and validate a tuning file."""
    location = pathlib.Path(path)
    try:
        raw = location.read_text()
    except OSError as exc:
        raise TuningFileError(f"{location}: cannot read tuning file: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TuningFileError(f"{location}: not valid JSON: {exc}") from exc
    return parse_tuning(payload, origin=str(location))


# --- environment -------------------------------------------------------------------------


def environment_overrides() -> dict[str, int]:
    """Positive integers from the override variables; anything else is ignored, as before."""
    overrides: dict[str, int] = {}
    for field, variable in (
        ("block_v", ENV_BLOCK_V),
        ("num_warps", ENV_NUM_WARPS),
        ("num_stages", ENV_NUM_STAGES),
    ):
        raw = os.environ.get(variable, "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            overrides[field] = value
    return overrides


@dataclasses.dataclass
class _State:
    loaded: bool = False
    env: dict[str, int] = dataclasses.field(default_factory=dict)
    tuning: dict[tuple[str, str, str], LaunchConfig] = dataclasses.field(default_factory=dict)
    tuning_path: str | None = None
    resolved: dict[tuple[str, tuple[int, int], int], LaunchConfig] = dataclasses.field(
        default_factory=dict
    )


_state = _State()
_lock = threading.Lock()


def _ensure_loaded() -> _State:
    if _state.loaded:
        return _state
    with _lock:
        if not _state.loaded:
            path = os.environ.get(TUNING_FILE_ENV, "").strip() or None
            _state.tuning = load_tuning_file(path) if path else {}
            _state.tuning_path = path
            _state.env = environment_overrides()
            _state.loaded = True
    return _state


def reload() -> dict[str, Any]:
    """Re-read the environment and tuning file, dropping every cached resolution.

    Raises :class:`TuningFileError` when the tuning file is invalid, which a serving host
    calls at startup so a bad file stops the substitution loudly instead of mid-request.
    """
    with _lock:
        _state.loaded = False
        _state.resolved.clear()
        _state.tuning = {}
        _state.env = {}
        _state.tuning_path = None
    state = _ensure_loaded()
    return describe(state)


def describe(state: _State | None = None) -> dict[str, Any]:
    state = state or _ensure_loaded()
    return {
        "environment_overrides": dict(state.env),
        "tuning_file": state.tuning_path,
        "tuning_entries": len(state.tuning),
    }


def table_entry(kernel: str, capability: tuple[int, int], batch: int) -> LaunchConfig:
    """The built-in entry for a capability and batch, falling back to the default."""
    if kernel not in KERNELS:
        raise ValueError(f"unknown kernel {kernel!r}")
    per_cc = TABLE[kernel].get(capability_key(capability))
    if per_cc is None:
        return DEFAULTS[kernel]
    return per_cc[batch_bucket(batch)]


def base_config(kernel: str, capability: tuple[int, int], batch: int) -> LaunchConfig:
    """Tuning file entry if present, else table entry, else default."""
    state = _ensure_loaded()
    key = (kernel, capability_key(capability), batch_bucket(batch))
    return state.tuning.get(key) or table_entry(kernel, capability, batch)


def resolve(
    kernel: str,
    capability: tuple[int, int],
    batch: int,
    *,
    block_v: int | None = None,
    num_warps: int | None = None,
    num_stages: int | None = None,
) -> LaunchConfig:
    """Pick the launch shape by the precedence in the module docstring."""
    if block_v is not None and num_warps is not None and num_stages is not None:
        # Fully specified: nothing to look up, and a sweep stays independent of whatever
        # tuning file or overrides the environment happens to carry.
        return LaunchConfig(block_v, num_warps, num_stages, False, "explicit")
    state = _ensure_loaded()
    explicit = block_v is not None or num_warps is not None or num_stages is not None
    cache_key = (kernel, tuple(capability), batch)
    if not explicit:
        cached = state.resolved.get(cache_key)
        if cached is not None:
            return cached

    base = base_config(kernel, capability, batch)
    env = state.env if kernel == KERNEL_PACKED else {}
    used_env = False

    def pick(field: str, argument: int | None) -> int | None:
        nonlocal used_env
        if argument is not None:
            return argument
        if field in env:
            used_env = True
            return env[field]
        return None

    chosen_block_v = pick("block_v", block_v)
    block_overridden = chosen_block_v is not None
    if chosen_block_v is None:
        chosen_block_v = base.block_v

    chosen_warps = pick("num_warps", num_warps)
    if chosen_warps is None:
        # A table entry's warp count belongs to its own tile; derive one for an overridden
        # tile rather than pair it with a shape it was not chosen for.
        chosen_warps = (
            legacy_num_warps(kernel, chosen_block_v) if block_overridden else base.num_warps
        )

    chosen_stages = pick("num_stages", num_stages)
    if chosen_stages is None:
        chosen_stages = base.num_stages

    chosen = (chosen_block_v, chosen_warps, chosen_stages)
    if chosen == (base.block_v, base.num_warps, base.num_stages):
        config = base
    else:
        layers = [
            name for name, used in (("explicit", explicit), ("environment", used_env)) if used
        ]
        config = LaunchConfig(
            *chosen,
            measured=False,
            source="+".join([*layers, base.source]),
            note=f"overrides applied on top of {base.source}",
        )
    if not explicit:
        state.resolved[cache_key] = config
    return config


def resolve_for_device(
    kernel: str,
    device: torch.device,
    batch: int,
    *,
    block_v: int | None = None,
    num_warps: int | None = None,
    num_stages: int | None = None,
) -> LaunchConfig:
    return resolve(
        kernel,
        device_capability(device),
        batch,
        block_v=block_v,
        num_warps=num_warps,
        num_stages=num_stages,
    )


__all__ = [
    "BATCH_BUCKETS",
    "BUCKET_LABELS",
    "DEFAULTS",
    "KERNEL_PACKED",
    "KERNEL_RECURRENT",
    "LaunchConfig",
    "TABLE",
    "TUNING_FILE_ENV",
    "TUNING_SCHEMA",
    "TuningFileError",
    "batch_bucket",
    "bf16_unsupported_reason",
    "device_capability",
    "load_tuning_file",
    "parse_tuning",
    "reload",
    "resolve",
    "resolve_for_device",
    "table_entry",
]
