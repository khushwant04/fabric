#!/usr/bin/env bash
#
# Bootstrap an Azure A10 GPU VM (Ubuntu 22.04 LTS server) for Fabric research runs.
#
# Scope: the two-A10 research environment from the roadmap. It prepares the host to
# produce A10 benchmark artifacts and to run the vLLM decode comparison.
#
# What this deliberately does NOT do:
#
#   * Install an NVIDIA driver. Azure NVIDIA GPU images ship one; nvidia-smi is
#     verified instead.
#   * Install CUDA, cuDNN, or NCCL through apt. The PyTorch wheel and vLLM's
#     dependencies provide the CUDA runtime, and apt versions would be a second,
#     conflicting source of truth.
#   * Install the newest PyTorch or vLLM. Fabric pins torch==2.8.0 and
#     triton==3.4.0, and vllm==0.11.0 is the newest release on that torch. Running
#     A10 measurements on a different toolchain would make them non-comparable
#     with the committed artifacts, which record their exact inputs.
#   * Reference any model identity. Supply it at run time through FABRIC_MODEL.
#
# Usage:
#   ./scripts/bootstrap-a10.sh
#
# Idempotent: re-running is safe and skips completed steps.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_VERSION="3.12"

# Driver floor for the cu128 wheels used by torch 2.8.0.
MINIMUM_DRIVER_MAJOR=525

# Expected research hardware. A mismatch is a warning, not a failure: the harness
# itself refuses to record a run under the wrong target.
EXPECTED_GPU_FRAGMENT="A10"
EXPECTED_COMPUTE_CAPABILITY="8.6"

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

require_sudo() {
    if [ "$(id -u)" -eq 0 ]; then
        SUDO=""
    elif command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        die "sudo is required to install system packages"
    fi
}

# --------------------------------------------------------------------------
log "Host"
# --------------------------------------------------------------------------

if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    echo "distribution: ${PRETTY_NAME:-unknown}"
    case "${VERSION_ID:-}" in
        22.04) ;;
        *) warn "this script targets Ubuntu 22.04 LTS; found ${VERSION_ID:-unknown}" ;;
    esac
fi
echo "kernel:       $(uname -r)"
echo "repository:   $REPO_ROOT"

# --------------------------------------------------------------------------
log "System packages"
# --------------------------------------------------------------------------

require_sudo
export DEBIAN_FRONTEND=noninteractive

$SUDO apt-get update -qq
# Build tooling for any source wheels, plus the operator conveniences that make a
# long benchmark session on a remote VM bearable.
$SUDO apt-get install -y -qq \
    build-essential \
    ca-certificates \
    curl \
    git \
    htop \
    libffi-dev \
    libssl-dev \
    nvtop \
    pkg-config \
    python3-dev \
    tmux \
    unzip \
    wget

# --------------------------------------------------------------------------
log "NVIDIA driver"
# --------------------------------------------------------------------------

command -v nvidia-smi >/dev/null 2>&1 || die "nvidia-smi not found; use an Azure NVIDIA GPU VM image"

nvidia-smi --query-gpu=index,name,driver_version,memory.total,compute_cap \
    --format=csv,noheader || die "nvidia-smi failed; the driver is not usable"

DRIVER_VERSION="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | tr -d ' ')"
DRIVER_MAJOR="${DRIVER_VERSION%%.*}"
if [ "${DRIVER_MAJOR:-0}" -lt "$MINIMUM_DRIVER_MAJOR" ]; then
    die "driver $DRIVER_VERSION is below the $MINIMUM_DRIVER_MAJOR floor required by cu128 wheels"
fi
echo "driver $DRIVER_VERSION satisfies the cu128 floor ($MINIMUM_DRIVER_MAJOR)"

GPU_COUNT="$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l)"
GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
COMPUTE_CAP="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d ' ')"

case "$GPU_NAME" in
    *"$EXPECTED_GPU_FRAGMENT"*) echo "hardware matches the research target: $GPU_NAME" ;;
    *) warn "expected an $EXPECTED_GPU_FRAGMENT; found '$GPU_NAME'. Artifacts must then be recorded under a different --target." ;;
esac
[ "$COMPUTE_CAP" = "$EXPECTED_COMPUTE_CAPABILITY" ] || \
    warn "expected compute capability $EXPECTED_COMPUTE_CAPABILITY, found $COMPUTE_CAP"
echo "GPUs visible: $GPU_COUNT"
[ "$GPU_COUNT" -ge 2 ] || warn "the research profile assumes two A10s; found $GPU_COUNT"

# --------------------------------------------------------------------------
log "CUDA compiler (optional)"
# --------------------------------------------------------------------------

if command -v nvcc >/dev/null 2>&1; then
    nvcc --version | tail -2
else
    echo "nvcc is absent, which is fine: the CUDA runtime comes from the Python wheels."
fi

# --------------------------------------------------------------------------
log "uv"
# --------------------------------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null 2>&1 || die "uv is not on PATH after installation"
uv --version
# Ubuntu 22.04 ships Python 3.10; uv fetches a managed 3.12 rather than needing a PPA.
uv python install "$PYTHON_VERSION" >/dev/null 2>&1 || true

# --------------------------------------------------------------------------
log "Model and dataset cache"
# --------------------------------------------------------------------------

# Keep large downloads off the OS disk. /mnt is the ephemeral NVMe on Azure GPU VMs.
CACHE_ROOT="${FABRIC_CACHE_ROOT:-/mnt/huggingface}"
CACHE_PARENT="$(dirname "$CACHE_ROOT")"

if [ -d "$CACHE_PARENT" ]; then
    $SUDO mkdir -p "$CACHE_ROOT/hub"
    $SUDO chown -R "$(id -u):$(id -g)" "$CACHE_ROOT"
    echo "cache root: $CACHE_ROOT ($(df -h "$CACHE_PARENT" | awk 'NR==2 {print $4}') free)"

    PROFILE="$HOME/.bashrc"
    if ! grep -q "HF_HOME=$CACHE_ROOT" "$PROFILE" 2>/dev/null; then
        {
            echo ""
            echo "# Fabric: keep model weights off the OS disk"
            echo "export HF_HOME=$CACHE_ROOT"
            echo "export HF_HUB_CACHE=$CACHE_ROOT/hub"
        } >> "$PROFILE"
        echo "exported HF_HOME and HF_HUB_CACHE in $PROFILE"
    fi
    export HF_HOME="$CACHE_ROOT"
    export HF_HUB_CACHE="$CACHE_ROOT/hub"
else
    warn "$CACHE_PARENT does not exist; leaving the cache on the OS disk. Set FABRIC_CACHE_ROOT to override."
fi

# --------------------------------------------------------------------------
log "Runtime environment (kernels, harness, artifacts)"
# --------------------------------------------------------------------------

cd "$REPO_ROOT/runtime"
[ -d .venv ] || uv venv --python "$PYTHON_VERSION" .venv

# torch first, explicitly from the cu128 index, so the pinned build is the one the
# artifacts will record. triton arrives with it but is pinned for the same reason.
uv pip install --quiet --python .venv/bin/python \
    "torch==2.8.0" "triton==3.4.0" \
    --index-url https://download.pytorch.org/whl/cu128
# dev for the test suite, baselines for the pinned optimized FLA comparison.
uv pip install --quiet --python .venv/bin/python -e '.[dev,baselines]'

.venv/bin/python - <<'PY'
import torch
import triton

print("torch:", torch.__version__, "| triton:", triton.__version__)
print("cuda available:", torch.cuda.is_available(), "| cuda runtime:", torch.version.cuda)
print("bf16 supported:", torch.cuda.is_bf16_supported())
for index in range(torch.cuda.device_count()):
    properties = torch.cuda.get_device_properties(index)
    print(
        f"gpu {index}: {properties.name} | cc={properties.major}.{properties.minor} "
        f"| vram={properties.total_memory / 1024**3:.1f} GiB"
    )
PY

log "Runtime test suite (no GPU required)"
.venv/bin/python -m pytest -q

# --------------------------------------------------------------------------
log "Serving environment (vLLM comparison)"
# --------------------------------------------------------------------------

cd "$REPO_ROOT/serving"
[ -d .venv ] || uv venv --python "$PYTHON_VERSION" .venv

uv pip install --quiet --python .venv/bin/python \
    "torch==2.8.0" "triton==3.4.0" \
    --index-url https://download.pytorch.org/whl/cu128
# vllm==0.11.0 is pinned in pyproject.toml; it is the newest release on torch 2.8.0.
uv pip install --quiet --python .venv/bin/python -e .

.venv/bin/python - <<'PY'
import torch
import vllm

print("vllm:", vllm.__version__, "| torch:", torch.__version__, "| cuda:", torch.version.cuda)
print("nccl available:", torch.distributed.is_nccl_available())
if torch.distributed.is_nccl_available():
    print("nccl version:", torch.cuda.nccl.version())
PY

log "Serving test suite"
.venv/bin/python -m pytest -q

# --------------------------------------------------------------------------
log "GPU topology"
# --------------------------------------------------------------------------

nvidia-smi topo -m || warn "topology query failed"

# --------------------------------------------------------------------------
log "Ready"
# --------------------------------------------------------------------------

cat <<EOF

Both environments are installed on the pinned toolchain:

  runtime  $REPO_ROOT/runtime/.venv    torch 2.8.0 + triton 3.4.0 + FLA baseline
  serving  $REPO_ROOT/serving/.venv    vllm 0.11.0 on the same torch

Record A10 research artifacts:

  ./scripts/run-a10-suite.sh

Serve a model for manual experiments (identity is supplied here, never committed):

  export FABRIC_MODEL=<org/model>
  source $REPO_ROOT/serving/.venv/bin/activate
  # Default research configuration is two independent single-GPU replicas.
  CUDA_VISIBLE_DEVICES=0 vllm serve "\$FABRIC_MODEL" --port 8000 --dtype bfloat16 \\
      --gpu-memory-utilization 0.90 --max-model-len 2048 --max-num-seqs 64
  CUDA_VISIBLE_DEVICES=1 vllm serve "\$FABRIC_MODEL" --port 8001 --dtype bfloat16 \\
      --gpu-memory-utilization 0.90 --max-model-len 2048 --max-num-seqs 64

  # Tensor-parallel size two is a separate paper experiment, not the default,
  # and it introduces NCCL on a model that fits one GPU:
  #   vllm serve "\$FABRIC_MODEL" --tensor-parallel-size 2 ...

EOF
