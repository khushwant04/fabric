"""Execution-environment capture.

Every benchmark artifact records the exact inputs that produced it: dependency
versions, GPU identity, and repository state. Capture never raises when CUDA is
absent so the pipeline stays testable on a CPU-only host.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from typing import Any

RUNTIME_ROOT_MARKER = "pyproject.toml"


def _command_output(args: list[str]) -> str | None:
    """Return trimmed stdout, or ``None`` when the tool is unavailable."""
    executable = shutil.which(args[0])
    if executable is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argument vector, no shell
            [executable, *args[1:]],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


#: Paths whose contents can change a measurement. Scoped deliberately: an artifact
#: is marked dirty to say "this result does not correspond to any commit", and an
#: edit to the frontend or to documentation cannot change a kernel timing. Checking
#: the whole tree marked every artifact dirty on any machine with unrelated work in
#: progress, which made the flag useless precisely when it mattered.
MEASUREMENT_PATHS = ("runtime", "serving")


def _git_state() -> dict[str, Any]:
    commit = _command_output(["git", "rev-parse", "HEAD"])

    # Anchored at the repository root. Pathspecs are resolved relative to the working
    # directory, and the harness is documented to run from serving/ so the vLLM
    # baseline is importable. From there "runtime" and "serving" matched nothing, git
    # returned no output, and every artifact reported a clean tree regardless of what
    # had been edited: the flag that exists to guarantee a result corresponds to a
    # commit was silently always reassuring.
    toplevel = _command_output(["git", "rev-parse", "--show-toplevel"])
    location = ["-C", toplevel] if toplevel else []

    # Untracked artifacts are excluded: writing a result must not make the result
    # that is being written look untrustworthy.
    status = _command_output(
        [
            "git",
            *location,
            "status",
            "--porcelain",
            "--untracked-files=no",
            "--",
            *MEASUREMENT_PATHS,
        ]
    )
    return {
        "commit": commit,
        # ``None`` status means git was unavailable, which is not the same as clean.
        "dirty": None if status is None and commit is None else bool(status),
        "dirty_scope": list(MEASUREMENT_PATHS),
    }


def _driver_version() -> str | None:
    return _command_output(
        ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"]
    )


#: Queried per run so a reader can see whether the GPU was throttling. Timings on a
#: laptop part drift substantially under sustained load, and a comparison run while
#: clocks are down is not comparable with one run cold. Without this the artifact
#: records the number but not the condition that produced it.
_THERMAL_FIELDS = (
    "clocks.sm",
    "clocks.max.sm",
    "temperature.gpu",
    "power.draw",
    "power.limit",
)


def _thermal_state() -> dict[str, Any] | None:
    """Clocks, temperature, and power at the time of the run."""
    output = _command_output(
        [
            "nvidia-smi",
            f"--query-gpu={','.join(_THERMAL_FIELDS)}",
            "--format=csv,noheader,nounits",
        ]
    )
    if output is None:
        return None

    values = [item.strip() for item in output.splitlines()[0].split(",")]
    if len(values) != len(_THERMAL_FIELDS):
        return None

    def number(raw: str) -> float | None:
        try:
            return float(raw)
        except ValueError:
            return None

    state = {
        "sm_clock_mhz": number(values[0]),
        "sm_clock_max_mhz": number(values[1]),
        "temperature_c": number(values[2]),
        "power_draw_w": number(values[3]),
        "power_limit_w": number(values[4]),
    }
    if state["sm_clock_mhz"] and state["sm_clock_max_mhz"]:
        # Recorded rather than judged: a threshold would be a guess, and the ratio is
        # what a reader needs to compare two runs.
        state["sm_clock_fraction_of_max"] = state["sm_clock_mhz"] / state["sm_clock_max_mhz"]
    return state


def _gpu_state() -> dict[str, Any]:
    try:
        import torch
    except ImportError:
        return {
            "torch_version": None,
            "torch_cuda_version": None,
            "triton_version": None,
            "cuda_available": False,
            "name": None,
            "compute_capability": None,
            "memory_bytes": None,
        }

    try:
        import triton

        triton_version = triton.__version__
    except ImportError:
        triton_version = None

    state: dict[str, Any] = {
        "torch_version": torch.__version__,
        "torch_cuda_version": torch.version.cuda,
        "triton_version": triton_version,
        "cuda_available": bool(torch.cuda.is_available()),
        "name": None,
        "compute_capability": None,
        "memory_bytes": None,
    }
    if state["cuda_available"]:
        properties = torch.cuda.get_device_properties(0)
        state["name"] = properties.name
        state["compute_capability"] = f"{properties.major}.{properties.minor}"
        state["memory_bytes"] = properties.total_memory
    return state


def _optional_version(module_name: str) -> str | None:
    """Return an installed library's version without requiring it."""
    try:
        module = __import__(module_name)
    except ImportError:
        return None
    return getattr(module, "__version__", "unknown")


def capture() -> dict[str, Any]:
    """Return the immutable inputs describing this execution environment."""
    gpu = _gpu_state()
    return {
        # Thermal state at run time, so a throttled result is identifiable as one.
        "thermal": _thermal_state(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "torch_version": gpu.pop("torch_version"),
        "torch_cuda_version": gpu.pop("torch_cuda_version"),
        "triton_version": gpu.pop("triton_version"),
        "cuda_available": gpu.pop("cuda_available"),
        "gpu": gpu,
        "driver_version": _driver_version(),
        # Baseline libraries named by the artifact contract; None when absent.
        "fla_version": _optional_version("fla"),
        "vllm_version": _optional_version("vllm"),
        "git": _git_state(),
    }
