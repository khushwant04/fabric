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
    # Untracked artifacts are excluded: writing a result must not make the result
    # that is being written look untrustworthy.
    status = _command_output(
        [
            "git",
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
