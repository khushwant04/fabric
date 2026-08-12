#!/usr/bin/env bash
#
# Bootstrap an Azure A10 GPU VM for Fabric research runs, from a bare
# Ubuntu 22.04 LTS server image with no NVIDIA driver installed.
#
# Target host: Standard_NV72ads_A10_v5 (2 x A10-24Q vGPU, 24 GiB each).
#
# Phases, in order. Each is idempotent, so re-running skips completed work:
#
#   0  preflight        OS, Secure Boot, GPU visibility on the PCI bus, disks
#   1  system packages  build tooling, DKMS, kernel headers
#   2  NVIDIA driver    Azure GRID .run installer, then a reboot if needed
#   3  CUDA toolkit     12.8 from NVIDIA's repository (not Ubuntu's package)
#   4  uv
#   5  model cache      moved off the 128 GiB OS disk onto the data disk
#   6  runtime env      pinned torch/triton + FLA baseline, tests
#   7  serving env      vllm 0.11.0 on the same torch, tests
#   8  validation       driver, nvcc, BF16 execution, NCCL, topology
#
# The driver install needs a reboot before the kernel module is usable. When
# that happens the script stops and tells you to reboot and re-run; the second
# run detects the working driver and continues.
#
# Usage:
#   ./scripts/bootstrap-a10.sh
#   ./scripts/bootstrap-a10.sh --skip-driver          # driver already present
#   ./scripts/bootstrap-a10.sh --skip-cuda-toolkit    # wheels only, no nvcc
#
# Overrides:
#   FABRIC_DRIVER_URL, FABRIC_DRIVER_VERSION, FABRIC_CACHE_ROOT

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_VERSION="3.12"

# Azure publishes the GRID driver for the NVadsA10_v5 family. A generic
# datacenter or desktop driver is the wrong choice for this vGPU SKU, and
# Ubuntu's nvidia-driver-* packages are a different, conflicting source.
DRIVER_VERSION="${FABRIC_DRIVER_VERSION:-570.211.01}"
DRIVER_RUN="NVIDIA-Linux-x86_64-${DRIVER_VERSION}-grid-azure.run"
DRIVER_URL="${FABRIC_DRIVER_URL:-https://download.microsoft.com/download/2a04ca6a-9eec-40d9-9564-9cdea1ab795f/NVIDIA-Linux-x86_64-570.211.01-grid-azure.run}"
DRIVER_DOWNLOAD_DIR="${HOME}/nvidia-driver"
MINIMUM_DRIVER_BYTES=$((100 * 1024 * 1024))

# CUDA 12.8 matches the driver's reported CUDA version and the cu128 wheels the
# Fabric runtime pins. Installed as cuda-toolkit-12-8, never as `cuda`, because
# the meta-package pulls driver components that would fight the GRID driver.
CUDA_VERSION="12.8"
CUDA_HOME="/usr/local/cuda-${CUDA_VERSION}"
CUDA_APT_PACKAGE="cuda-toolkit-12-8"
CUDA_KEYRING_URL="https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb"

# The two A10s present as "NVIDIA A10-24Q" under vGPU. A mismatch is a warning:
# the benchmark harness itself refuses to record a run under the wrong target.
EXPECTED_GPU_FRAGMENT="A10"
EXPECTED_GPU_COUNT=2

SKIP_DRIVER=0
SKIP_CUDA_TOOLKIT=0

log()   { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info()  { printf '   %s\n' "$*"; }
warn()  { printf '\033[33mWARNING: %s\033[0m\n' "$*" >&2; }
die()   { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --skip-driver) SKIP_DRIVER=1 ;;
        --skip-cuda-toolkit) SKIP_CUDA_TOOLKIT=1 ;;
        -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    die "sudo is required to install the driver and system packages"
fi

driver_works() {
    command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1
}

# --------------------------------------------------------------------------
log "Phase 0: preflight"
# --------------------------------------------------------------------------

if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    info "distribution: ${PRETTY_NAME:-unknown}"
    case "${VERSION_ID:-}" in
        22.04) ;;
        *) warn "this script targets Ubuntu 22.04 LTS; found ${VERSION_ID:-unknown}. The CUDA repository and driver pin are chosen for 22.04." ;;
    esac
fi
info "kernel:       $(uname -r)"
info "repository:   $REPO_ROOT"

# The GRID driver builds an unsigned kernel module, which Secure Boot rejects.
if command -v mokutil >/dev/null 2>&1; then
    SECURE_BOOT="$(mokutil --sb-state 2>/dev/null || echo 'unknown')"
    info "secure boot:  $SECURE_BOOT"
    case "$SECURE_BOOT" in
        *enabled*) die "Secure Boot is enabled; the GRID kernel module will not load. Recreate the VM with Security type 'Standard' (Secure Boot and vTPM disabled)." ;;
    esac
fi

# GPUs are visible on the PCI bus before any driver exists. If they are not
# here, no amount of driver installation will help.
if command -v lspci >/dev/null 2>&1; then
    PCI_GPUS="$(lspci | grep -ci nvidia || true)"
    info "NVIDIA devices on the PCI bus: $PCI_GPUS"
    [ "$PCI_GPUS" -gt 0 ] || die "no NVIDIA device found by lspci; this is not a GPU VM"
    lspci | grep -i nvidia | sed 's/^/   /'
fi

info "disks:"
df -h --output=target,size,avail / /mnt 2>/dev/null | sed 's/^/   /' || df -h / | sed 's/^/   /'

# --------------------------------------------------------------------------
log "Phase 1: system packages"
# --------------------------------------------------------------------------

export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq
# dkms and the running kernel's headers are what let the driver rebuild its
# module across kernel upgrades. mokutil reports Secure Boot state.
$SUDO apt-get install -y -qq \
    build-essential \
    ca-certificates \
    curl \
    dkms \
    git \
    htop \
    libffi-dev \
    libssl-dev \
    "linux-headers-$(uname -r)" \
    mokutil \
    nvtop \
    pciutils \
    pkg-config \
    python3-dev \
    tmux \
    unzip \
    wget
info "installed build tooling, dkms, and headers for $(uname -r)"

# --------------------------------------------------------------------------
log "Phase 2: NVIDIA driver"
# --------------------------------------------------------------------------

if [ "$SKIP_DRIVER" -eq 1 ]; then
    info "skipped by request"
    driver_works || die "--skip-driver was passed but nvidia-smi does not work"
elif driver_works; then
    INSTALLED_DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | tr -d ' ')"
    info "driver $INSTALLED_DRIVER already installed and responding"
    [ "$INSTALLED_DRIVER" = "$DRIVER_VERSION" ] || \
        warn "installed driver $INSTALLED_DRIVER differs from the pinned $DRIVER_VERSION; artifacts will record what is actually present"
else
    info "no working driver; installing the Azure GRID driver $DRIVER_VERSION"
    mkdir -p "$DRIVER_DOWNLOAD_DIR"
    cd "$DRIVER_DOWNLOAD_DIR"

    if [ ! -s "$DRIVER_RUN" ]; then
        info "downloading $DRIVER_RUN"
        wget -q --show-progress -O "$DRIVER_RUN.partial" "$DRIVER_URL"
        mv "$DRIVER_RUN.partial" "$DRIVER_RUN"
    else
        info "reusing previously downloaded $DRIVER_RUN"
    fi

    DRIVER_BYTES="$(stat -c %s "$DRIVER_RUN")"
    [ "$DRIVER_BYTES" -ge "$MINIMUM_DRIVER_BYTES" ] || \
        die "$DRIVER_RUN is only $DRIVER_BYTES bytes; the download failed or the URL returned an error page"
    chmod +x "$DRIVER_RUN"

    # --silent accepts the licence and skips the ncurses prompts, which would
    # otherwise block an unattended run on the X11 path and Vulkan warnings.
    # --dkms rebuilds the module on kernel upgrades. 32-bit compatibility
    # libraries are not needed on a headless inference host.
    info "running the installer (this takes a few minutes)"
    $SUDO sh "./$DRIVER_RUN" \
        --silent \
        --dkms \
        --no-install-compat32-libs \
        || die "driver installation failed; inspect /var/log/nvidia-installer.log"

    cd "$REPO_ROOT"
    $SUDO modprobe nvidia 2>/dev/null || true

    if driver_works; then
        info "driver is live without a reboot"
    else
        cat <<EOF

The driver installed but the kernel module is not serving requests yet.

  1. sudo reboot
  2. re-run this script; it will detect the working driver and continue

EOF
        exit 0
    fi
fi

log "Driver status"
nvidia-smi --query-gpu=index,name,driver_version,memory.total,compute_cap \
    --format=csv,noheader | sed 's/^/   /'

GPU_COUNT="$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l)"
GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
case "$GPU_NAME" in
    *"$EXPECTED_GPU_FRAGMENT"*) info "hardware matches the research target: $GPU_NAME" ;;
    *) warn "expected an $EXPECTED_GPU_FRAGMENT; found '$GPU_NAME'. Artifacts must then be recorded under a different --target." ;;
esac
[ "$GPU_COUNT" -eq "$EXPECTED_GPU_COUNT" ] || \
    warn "the research profile assumes $EXPECTED_GPU_COUNT A10s; found $GPU_COUNT"

# --------------------------------------------------------------------------
log "Phase 3: CUDA toolkit $CUDA_VERSION"
# --------------------------------------------------------------------------

if [ "$SKIP_CUDA_TOOLKIT" -eq 1 ]; then
    info "skipped by request; the Python wheels carry their own CUDA runtime"
elif [ -x "$CUDA_HOME/bin/nvcc" ]; then
    info "already installed: $("$CUDA_HOME/bin/nvcc" --version | tail -1)"
else
    # NVIDIA's repository, not Ubuntu's nvidia-cuda-toolkit, which ships a
    # different CUDA version.
    KEYRING_DEB="$DRIVER_DOWNLOAD_DIR/$(basename "$CUDA_KEYRING_URL")"
    mkdir -p "$DRIVER_DOWNLOAD_DIR"
    [ -s "$KEYRING_DEB" ] || wget -q -O "$KEYRING_DEB" "$CUDA_KEYRING_URL"
    $SUDO dpkg -i "$KEYRING_DEB" >/dev/null
    $SUDO apt-get update -qq
    info "installing $CUDA_APT_PACKAGE (several GiB)"
    $SUDO apt-get install -y -qq "$CUDA_APT_PACKAGE"
fi

if [ -x "$CUDA_HOME/bin/nvcc" ]; then
    PROFILE="$HOME/.bashrc"
    if ! grep -q "CUDA_HOME=$CUDA_HOME" "$PROFILE" 2>/dev/null; then
        {
            echo ""
            echo "# Fabric: CUDA $CUDA_VERSION toolkit"
            echo "export CUDA_HOME=$CUDA_HOME"
            echo 'export PATH=$CUDA_HOME/bin:$PATH'
            echo 'export LD_LIBRARY_PATH=$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}'
        } >> "$PROFILE"
        info "exported CUDA_HOME, PATH, and LD_LIBRARY_PATH in $PROFILE"
    fi
    export CUDA_HOME
    export PATH="$CUDA_HOME/bin:$PATH"
    export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"
    info "nvcc: $(nvcc --version | tail -1)"
    # nvidia-smi reports the driver's maximum CUDA API; nvcc reports the
    # toolkit. They are different numbers and need not match.
    info "driver CUDA API: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
fi

# --------------------------------------------------------------------------
log "Phase 4: uv"
# --------------------------------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null 2>&1 || die "uv is not on PATH after installation"
info "uv: $(uv --version)"
# Ubuntu 22.04 ships Python 3.10; uv fetches a managed 3.12 rather than a PPA.
uv python install "$PYTHON_VERSION" >/dev/null 2>&1 || true

# --------------------------------------------------------------------------
log "Phase 5: model and dataset cache"
# --------------------------------------------------------------------------

# Keep large downloads off the 128 GiB OS disk. On this SKU the data disk is
# already formatted and mounted at /mnt.
CACHE_ROOT="${FABRIC_CACHE_ROOT:-/mnt/huggingface}"
CACHE_PARENT="$(dirname "$CACHE_ROOT")"

if [ ! -d "$CACHE_PARENT" ]; then
    warn "$CACHE_PARENT does not exist; leaving the cache on the OS disk. Set FABRIC_CACHE_ROOT to override."
else
    # A directory called /mnt that lives on the root filesystem is not the data
    # disk, and filling it would exhaust the 128 GiB OS disk.
    ROOT_DEVICE="$(df --output=source / | tail -1)"
    CACHE_DEVICE="$(df --output=source "$CACHE_PARENT" | tail -1)"
    if [ "$ROOT_DEVICE" = "$CACHE_DEVICE" ]; then
        warn "$CACHE_PARENT is on the OS disk ($ROOT_DEVICE), not a separate data disk."
        warn "Model weights would consume the OS disk. Mount the data disk at $CACHE_PARENT or set FABRIC_CACHE_ROOT."
    fi

    $SUDO mkdir -p "$CACHE_ROOT/hub"
    $SUDO chown -R "$(id -u):$(id -g)" "$CACHE_ROOT"
    info "cache root: $CACHE_ROOT on $CACHE_DEVICE ($(df -h "$CACHE_PARENT" | awk 'NR==2 {print $4}') free)"

    PROFILE="$HOME/.bashrc"
    if ! grep -q "HF_HOME=$CACHE_ROOT" "$PROFILE" 2>/dev/null; then
        {
            echo ""
            echo "# Fabric: keep model weights off the OS disk"
            echo "export HF_HOME=$CACHE_ROOT"
            echo "export HF_HUB_CACHE=$CACHE_ROOT/hub"
        } >> "$PROFILE"
        info "exported HF_HOME and HF_HUB_CACHE in $PROFILE"
    fi
    export HF_HOME="$CACHE_ROOT"
    export HF_HUB_CACHE="$CACHE_ROOT/hub"
fi

# --------------------------------------------------------------------------
log "Phase 6: runtime environment (kernels, harness, artifacts)"
# --------------------------------------------------------------------------

cd "$REPO_ROOT/runtime"
[ -d .venv ] || uv venv --python "$PYTHON_VERSION" .venv

# torch first, explicitly from the cu128 index, so the pinned build is the one
# the artifacts record. Installing current releases instead would put A10
# measurements on a different toolchain from the committed RTX artifacts.
uv pip install --quiet --python .venv/bin/python \
    "torch==2.8.0" "triton==3.4.0" \
    --index-url https://download.pytorch.org/whl/cu128
# dev for the test suite, baselines for the pinned optimized FLA comparison.
uv pip install --quiet --python .venv/bin/python -e '.[dev,baselines]'

.venv/bin/python - <<'PY'
import torch
import triton

print(f"   torch: {torch.__version__} | triton: {triton.__version__}")
print(f"   cuda available: {torch.cuda.is_available()} | cuda runtime: {torch.version.cuda}")
print(f"   bf16 supported: {torch.cuda.is_bf16_supported()}")
for index in range(torch.cuda.device_count()):
    p = torch.cuda.get_device_properties(index)
    print(
        f"   gpu {index}: {p.name} | cc={p.major}.{p.minor} "
        f"| vram={p.total_memory / 1024**3:.1f} GiB"
    )
PY

log "Runtime test suite (no GPU required)"
.venv/bin/python -m pytest -q

# --------------------------------------------------------------------------
log "Phase 7: serving environment (vLLM comparison)"
# --------------------------------------------------------------------------

cd "$REPO_ROOT/serving"
[ -d .venv ] || uv venv --python "$PYTHON_VERSION" .venv

uv pip install --quiet --python .venv/bin/python \
    "torch==2.8.0" "triton==3.4.0" \
    --index-url https://download.pytorch.org/whl/cu128
# vllm==0.11.0 is pinned in pyproject.toml: the newest release on torch 2.8.0.
# Later releases move to torch 2.9+ and would fork the toolchain.
uv pip install --quiet --python .venv/bin/python -e .

.venv/bin/python - <<'PY'
import torch
import vllm

print(f"   vllm: {vllm.__version__} | torch: {torch.__version__} | cuda: {torch.version.cuda}")
print(f"   nccl available: {torch.distributed.is_nccl_available()}")
if torch.distributed.is_nccl_available():
    print(f"   nccl version: {torch.cuda.nccl.version()}")
PY

log "Serving test suite"
.venv/bin/python -m pytest -q

# --------------------------------------------------------------------------
log "Phase 8: validation"
# --------------------------------------------------------------------------

info "BF16 execution on every GPU (device visibility is not proof of execution)"
cd "$REPO_ROOT/runtime"
.venv/bin/python - <<'PY'
import time

import torch

for index in range(torch.cuda.device_count()):
    a = torch.randn(4096, 4096, device=f"cuda:{index}", dtype=torch.bfloat16)
    b = torch.randn_like(a)
    for _ in range(10):
        c = a @ b
    torch.cuda.synchronize(index)

    started = time.perf_counter()
    for _ in range(100):
        c = a @ b
    torch.cuda.synchronize(index)
    elapsed = (time.perf_counter() - started) / 100 * 1e3

    print(f"   gpu {index}: {torch.cuda.get_device_name(index)} | {c.dtype} | {elapsed:.3f} ms")
    del a, b, c
    torch.cuda.empty_cache()
PY

info "GPU topology (SYS means peer traffic crosses PCIe and the CPU interconnect)"
nvidia-smi topo -m 2>/dev/null | sed 's/^/   /' || warn "topology query failed"

# --------------------------------------------------------------------------
log "Ready"
# --------------------------------------------------------------------------

cat <<EOF

Provisioned from a bare Ubuntu 22.04 host:

  driver    $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1) (Azure GRID, DKMS)
  toolkit   $([ -x "$CUDA_HOME/bin/nvcc" ] && "$CUDA_HOME/bin/nvcc" --version | tail -1 | sed 's/^ *//' || echo 'not installed')
  runtime   $REPO_ROOT/runtime/.venv    torch 2.8.0 + triton 3.4.0 + FLA baseline
  serving   $REPO_ROOT/serving/.venv    vllm 0.11.0 on the same torch
  cache     ${HF_HOME:-<OS disk>}

Record A10 research artifacts:

  ./scripts/run-a10-suite.sh

Serve a model for manual experiments (identity is supplied here, never committed):

  export FABRIC_MODEL=<org/model>
  source $REPO_ROOT/serving/.venv/bin/activate
  # Default research configuration is two independent single-GPU replicas.
  CUDA_VISIBLE_DEVICES=0 vllm serve "\$FABRIC_MODEL" --port 8000 --dtype bfloat16 \\
      --gpu-memory-utilization 0.88 --max-model-len 2048 --max-num-seqs 64
  CUDA_VISIBLE_DEVICES=1 vllm serve "\$FABRIC_MODEL" --port 8001 --dtype bfloat16 \\
      --gpu-memory-utilization 0.88 --max-model-len 2048 --max-num-seqs 64

  # Tensor-parallel size two is a separate paper experiment, not the default.
  # These A10s report SYS topology with no NVLink, so peer traffic crosses the
  # CPU interconnect and TP pays for it on a model that fits one GPU.

Counter-based profiling (achieved occupancy, warp efficiency, DRAM counters)
needs Nsight Compute with admin-enabled counters. If ncu reports
ERR_NVGPUCTRPERM, that is a host setting, not a code problem:
  https://developer.nvidia.com/ERR_NVGPUCTRPERM

EOF
