#!/usr/bin/env bash
# Driver preparation for Azure NVadsA10_v5 Ubuntu 22.04 hosts. This script
# installs no Kubernetes, CUDA toolkit, Python environments, or model weights.
# Reference: https://learn.microsoft.com/en-us/azure/virtual-machines/linux/n-series-driver-setup
# Azure's current page still lists 570.211.01 for this SKU despite a conflicting
# branch-support warning. Pin deliberately; override version and URL together.
set -Eeuo pipefail

DRIVER_VERSION=570.211.01
DRIVER_URL=https://download.microsoft.com/download/2a04ca6a-9eec-40d9-9564-9cdea1ab795f/NVIDIA-Linux-x86_64-570.211.01-grid-azure.run
DRIVER_SHA256=""
EXPECTED_GPUS=2
CLEAN_DRIVER=0
DIAGNOSE_ONLY=0
VERSION_OVERRIDE=0
URL_OVERRIDE=0
LOG_DIR=/var/log/fabric/a10-host
CACHE_DIR=/var/cache/fabric/nvidia-grid
STATE_DIR=/var/lib/fabric/a10-host
REBOOT_EXIT_CODE=194

usage() {
    cat <<'EOF'
Usage: sudo bash scripts/prepare-a10-host.sh [options]

Prepare only the Azure GRID driver on Ubuntu 22.04 NVadsA10_v5 VMs.
The default expects two NVIDIA A10-24Q GPUs. A healthy matching driver is reused.
Existing incompatible or broken driver installations require --clean-driver.

  --clean-driver            Remove scoped installed driver packages/.run driver
                            before reinstalling. Refuses active GPU workloads or
                            running k3s/k3s-agent; stop/decommission them yourself.
  --expected-gpus COUNT     Expected PCI and usable A10-24Q GPU count (default 2).
  --driver-version VERSION Override the GRID pin; must accompany --driver-url.
  --driver-url HTTPS_URL   Microsoft/NVIDIA GRID .run URL containing that version;
                            must accompany --driver-version.
  --driver-sha256 SHA256   Expected installer SHA-256, optional; otherwise the
                            NVIDIA installer's internal integrity check is used.
  --log-dir ABSOLUTE_PATH  Log directory (default /var/log/fabric/a10-host).
  --diagnose-only          Read-only OS/PCI/driver diagnostics; installs nothing.
  -h, --help               Print this help without changing the host.

Exit 0: driver is usable. Exit 194: reboot required, then rerun the same command.
Exit 1: prerequisite, safety, compatibility, or installation failure.
This script never reboots, stops k3s, purges the CUDA toolkit/container runtime,
or installs NVIDIA's generic desktop/datacenter driver on the Azure vGPU SKU.
The pinned Microsoft download is 570.211.01; verify Azure's published SKU support
before selecting another version. Both override values must agree.
EOF
}

log() { printf '[fabric-a10] %s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
need_value() { [ "$#" -ge 2 ] && [ -n "$2" ] || die "$1 requires a value"; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --clean-driver) CLEAN_DRIVER=1; shift ;;
        --diagnose-only) DIAGNOSE_ONLY=1; shift ;;
        --expected-gpus) need_value "$@"; EXPECTED_GPUS=$2; shift 2 ;;
        --driver-version) need_value "$@"; DRIVER_VERSION=$2; VERSION_OVERRIDE=1; shift 2 ;;
        --driver-url) need_value "$@"; DRIVER_URL=$2; URL_OVERRIDE=1; shift 2 ;;
        --driver-sha256) need_value "$@"; DRIVER_SHA256=$2; shift 2 ;;
        --log-dir) need_value "$@"; LOG_DIR=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[[ "$EXPECTED_GPUS" =~ ^[1-9][0-9]*$ ]] || die "--expected-gpus must be a positive integer"
[[ "$DRIVER_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid GRID driver version"
[ "$VERSION_OVERRIDE" -eq "$URL_OVERRIDE" ] || die "override --driver-version and --driver-url together"
DRIVER_RUN="NVIDIA-Linux-x86_64-${DRIVER_VERSION}-grid-azure.run"
case "${DRIVER_URL%%\?*}" in
    https://*/"$DRIVER_RUN") ;;
    *) die "--driver-url must be HTTPS and end with /$DRIVER_RUN" ;;
esac
if [ -n "$DRIVER_SHA256" ]; then
    [[ "$DRIVER_SHA256" =~ ^[[:xdigit:]]{64}$ ]] || die "--driver-sha256 requires 64 hex characters"
    DRIVER_SHA256=${DRIVER_SHA256,,}
fi
case "$LOG_DIR" in /*) ;; *) die "--log-dir must be an absolute path" ;; esac
[ "$DIAGNOSE_ONLY" -eq 0 ] || [ "$CLEAN_DRIVER" -eq 0 ] || die "--diagnose-only cannot be combined with --clean-driver"
[ "$(id -u)" -eq 0 ] || die "run with sudo bash scripts/prepare-a10-host.sh so GPU process inspection is complete"

# These checks need no installed NVIDIA tools and precede every host mutation.
[ -r /etc/os-release ] || die "cannot identify the operating system"
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 22.04 ] || die "requires Ubuntu 22.04; found ${PRETTY_NAME:-unknown}"
[ "$(uname -m)" = x86_64 ] || die "the Azure GRID installer requires x86_64"
[ -r /sys/class/dmi/id/sys_vendor ] || die "cannot verify Azure virtual-machine hardware"
case "$(cat /sys/class/dmi/id/sys_vendor)" in
    *Microsoft*) ;;
    *) die "requires an Azure Microsoft virtual machine; refusing driver changes on this host" ;;
esac

if [ "$DIAGNOSE_ONLY" -eq 0 ]; then
    [ ! -L "$LOG_DIR" ] && [ ! -L "$CACHE_DIR" ] && [ ! -L "$STATE_DIR" ] || die "setup directories must not be symlinks"
    install -d -m 0750 "$LOG_DIR" "$CACHE_DIR" "$STATE_DIR"
    LOG_FILE="$LOG_DIR/prepare-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
    exec > >(tee -a "$LOG_FILE") 2>&1
    log "Log: $LOG_FILE"
    exec 9>/run/lock/fabric-a10-host.lock
    flock -n 9 || die "another Fabric A10 driver setup is running"
    export DEBIAN_FRONTEND=noninteractive
    PREREQUISITES=(ca-certificates curl pciutils mokutil python3 psmisc)
    MISSING_PREREQUISITES=()
    for package in "${PREREQUISITES[@]}"; do
        if [ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)" != installed ]; then
            MISSING_PREREQUISITES+=("$package")
        fi
    done
    if [ "${#MISSING_PREREQUISITES[@]}" -gt 0 ]; then
        apt-get update
        apt-get install -y --no-install-recommends "${MISSING_PREREQUISITES[@]}"
    fi
else
    for tool in curl lspci mokutil python3 fuser; do
        command -v "$tool" >/dev/null 2>&1 || die "diagnostics need $tool; install prerequisite tools or run default setup"
    done
fi

KERNEL=$(uname -r)
BOOT_ID=$(cat /proc/sys/kernel/random/boot_id)
log "Host: ${PRETTY_NAME}; kernel $KERNEL; expected A10-24Q devices $EXPECTED_GPUS"

# Only the SKU field is requested; no metadata identity tokens or other VM fields
# are read or printed. Proxies must not intercept link-local Azure IMDS requests.
VM_SIZE=$(curl --noproxy '*' --fail --silent --show-error --connect-timeout 3 --max-time 10 \
    -H Metadata:true 'http://169.254.169.254/metadata/instance/compute/vmSize?api-version=2021-02-01&format=text') \
    || die "Azure IMDS is unavailable; cannot verify the NVadsA10_v5 SKU"
[[ "$VM_SIZE" =~ ^Standard_NV[0-9]+ads_A10_v5$ ]] || die "requires Standard_NV*ads_A10_v5; IMDS reports $VM_SIZE"
log "Azure SKU: $VM_SIZE"

SECURE_BOOT=$(mokutil --sb-state 2>&1) || true
case "$SECURE_BOOT" in
    *"SecureBoot enabled"*) die "disable Secure Boot in the Azure VM security configuration before installing unsigned GRID modules" ;;
    *"SecureBoot disabled"*|*"EFI variables are not supported"*) log "Secure Boot is disabled or this VM boots without EFI" ;;
    *) die "could not verify Secure Boot state: $SECURE_BOOT" ;;
esac
[ ! -d /sys/class/tpm/tpm0 ] || die "Azure GRID guidance requires vTPM disabled; update VM security configuration first"

PCI_GPUS=$(lspci -Dnn -d 10de: | awk '/\[(0300|0302)\]/ { print }')
PCI_COUNT=$(printf '%s\n' "$PCI_GPUS" | awk 'NF { count++ } END { print count+0 }')
[ "$PCI_COUNT" -eq "$EXPECTED_GPUS" ] || die "PCI exposes $PCI_COUNT NVIDIA GPU controllers; expected $EXPECTED_GPUS"
log "NVIDIA GPU PCI controllers:"
printf '%s\n' "$PCI_GPUS"

driver_package() {
    case "${1%%:*}" in
        nvidia-driver-[0-9]*|nvidia-headless-[0-9]*|nvidia-headless-no-dkms-[0-9]*|\
        nvidia-dkms-[0-9]*|nvidia-kernel-common-[0-9]*|nvidia-kernel-source-[0-9]*|\
        nvidia-utils-[0-9]*|nvidia-compute-utils-[0-9]*|nvidia-fabricmanager-[0-9]*|\
        libnvidia-compute-[0-9]*|libnvidia-encode-[0-9]*|libnvidia-decode-[0-9]*|\
        libnvidia-fbc[0-9]*-[0-9]*|libnvidia-gl-[0-9]*|libnvidia-cfg[0-9]*-[0-9]*|\
        libnvidia-extra-[0-9]*|libnvidia-common-[0-9]*|libnvidia-nscq-[0-9]*|\
        xserver-xorg-video-nvidia-[0-9]*|libcuda1-[0-9]*|\
        linux-modules-nvidia-[0-9]*|linux-objects-nvidia-[0-9]*|linux-signatures-nvidia-[0-9]*|\
        cuda-drivers|cuda-drivers-[0-9]*|nvidia-open|nvidia-open-[0-9]*) return 0 ;;
        *) return 1 ;;
    esac
}

DRIVER_PACKAGES=()
while IFS=$'\t' read -r status package; do
    case "$status" in installed|unpacked|half-installed|half-configured|triggers-awaited|triggers-pending|config-files) ;;
        *) continue ;;
    esac
    if driver_package "$package"; then DRIVER_PACKAGES+=("$package"); fi
done < <(dpkg-query -W -f='${db:Status-Status}\t${binary:Package}\n' 2>/dev/null)
if [ "${#DRIVER_PACKAGES[@]}" -gt 0 ]; then
    log "APT-managed driver components present (CUDA toolkit/container toolkit are excluded):"
    printf '  %s\n' "${DRIVER_PACKAGES[@]}"
fi

driver_healthy() {
    command -v nvidia-smi >/dev/null 2>&1 || return 1
    local report
    report=$(timeout 15s nvidia-smi --query-gpu=index,name,driver_version,memory.total,compute_cap --format=csv,noheader,nounits) || return 1
    log "Driver report (index, name, version, memory MiB, compute capability):"
    printf '%s\n' "$report"
    printf '%s\n' "$report" | awk -F, -v expected="$EXPECTED_GPUS" -v version="$DRIVER_VERSION" '
        { for (i=1; i<=NF; i++) { gsub(/^[ \t]+|[ \t]+$/, "", $i) } }
        NF != 5 || $2 != "NVIDIA A10-24Q" || $3 != version || $4+0 < 22000 || $5 != "8.6" { bad=1 }
        END { exit (bad || NR != expected) }
    '
}

gpu_processes_active() {
    local device pid comm users inspect_status
    local devices=()
    shopt -s nullglob
    for device in /dev/nvidia[0-9]* /dev/nvidiactl /dev/nvidia-uvm /dev/dri/renderD* /dev/dri/card*; do
        [ -e "$device" ] && devices+=("$device")
    done
    shopt -u nullglob
    [ "${#devices[@]}" -gt 0 ] || return 1
    command -v fuser >/dev/null 2>&1 || die "cannot verify GPU device users; install psmisc"
    if users=$(fuser "${devices[@]}" 2>/dev/null); then
        inspect_status=0
    else
        inspect_status=$?
    fi
    [ "$inspect_status" -le 1 ] || die "GPU device process inspection failed; refusing driver changes"
    for pid in $users; do
        [ -r "/proc/$pid/comm" ] || continue
        comm=$(cat "/proc/$pid/comm")
        case "$comm" in nvidia-persiste*) continue ;; esac
        log "GPU device is held by process $pid ($comm)"
        return 0
    done
    return 1
}

require_quiescent_host() {
    local service
    for service in k3s k3s-agent; do
        if systemctl is-active --quiet "$service"; then
            die "$service is running; drain/decommission GPU workloads and stop it yourself before driver installation"
        fi
    done
    for service in gdm3 lightdm sddm; do
        if systemctl is-active --quiet "$service"; then
            die "$service is using the graphical host; stop it yourself before driver installation"
        fi
    done
    gpu_processes_active && die "active GPU workloads prevent safe driver cleanup/installation"
    return 0
}

cuda_smoke() {
    # The driver exposes libcuda. A tiny PTX kernel verifies execution on every
    # device without installing torch, a CUDA toolkit, or a research environment.
    timeout 60s python3 - "$EXPECTED_GPUS" <<'PY'
import ctypes as C
import sys

try:
    cuda = C.CDLL("libcuda.so.1")
except OSError as exc:
    raise SystemExit(f"CUDA driver library unavailable: {exc}")

def bind(name, args):
    fn = getattr(cuda, name)
    fn.argtypes = args
    fn.restype = C.c_int
    return fn

ptr = C.c_void_p
u64 = C.c_uint64
init = bind("cuInit", [C.c_uint])
count_devices = bind("cuDeviceGetCount", [C.POINTER(C.c_int)])
get_device = bind("cuDeviceGet", [C.POINTER(C.c_int), C.c_int])
create = bind("cuCtxCreate_v2", [C.POINTER(ptr), C.c_uint, C.c_int])
destroy = bind("cuCtxDestroy_v2", [ptr])
allocate = bind("cuMemAlloc_v2", [C.POINTER(u64), C.c_size_t])
free = bind("cuMemFree_v2", [u64])
load = bind("cuModuleLoadData", [C.POINTER(ptr), ptr])
unload = bind("cuModuleUnload", [ptr])
get_function = bind("cuModuleGetFunction", [C.POINTER(ptr), ptr, C.c_char_p])
launch = bind("cuLaunchKernel", [ptr] + [C.c_uint] * 7 + [ptr, C.POINTER(ptr), C.POINTER(ptr)])
synchronize = bind("cuCtxSynchronize", [])
copy = bind("cuMemcpyDtoH_v2", [ptr, u64, C.c_size_t])

def check(result, operation):
    if result:
        raise RuntimeError(f"{operation}: CUDA driver error {result}")

ptx = C.create_string_buffer(b""".version 6.4
.target sm_52
.address_size 64
.visible .entry fabric_driver_probe(.param .u64 output) {
    .reg .b64 %rd;
    ld.param.u64 %rd, [output];
    st.global.u32 [%rd], 7;
    ret;
}
""")
check(init(0), "cuInit")
count = C.c_int()
check(count_devices(C.byref(count)), "cuDeviceGetCount")
if count.value != int(sys.argv[1]):
    raise SystemExit(f"CUDA exposes {count.value} devices; expected {sys.argv[1]}")
for index in range(count.value):
    device, context, module, function, memory = C.c_int(), ptr(), ptr(), ptr(), u64()
    try:
        check(get_device(C.byref(device), index), "cuDeviceGet")
        check(create(C.byref(context), 0, device), "cuCtxCreate")
        check(allocate(C.byref(memory), 4), "cuMemAlloc")
        check(load(C.byref(module), C.cast(ptx, ptr)), "cuModuleLoadData")
        check(get_function(C.byref(function), module, b"fabric_driver_probe"), "cuModuleGetFunction")
        params = (ptr * 1)(C.cast(C.byref(memory), ptr))
        check(launch(function, 1, 1, 1, 1, 1, 1, 0, None, params, None), "cuLaunchKernel")
        check(synchronize(), "cuCtxSynchronize")
        result = C.c_int()
        check(copy(C.byref(result), memory, 4), "cuMemcpyDtoH")
        if result.value != 7:
            raise RuntimeError(f"GPU {index}: incorrect kernel result {result.value}")
        print(f"CUDA execution passed on GPU {index}")
    finally:
        if memory.value:
            free(memory)
        if module.value:
            unload(module)
        if context.value:
            destroy(context)
PY
}

if [ "$DIAGNOSE_ONLY" -eq 1 ]; then
    if driver_healthy; then
        if gpu_processes_active; then
            log "GPU workloads are active; metadata is healthy, CUDA execution probe deferred"
        else
            cuda_smoke || die "CUDA execution failed; inspect the GRID driver and kernel logs"
        fi
        log "Read-only driver diagnostics complete"
        exit 0
    fi
    die "GRID $DRIVER_VERSION is not healthy on all expected A10-24Q devices"
fi

if driver_healthy && [ "${#DRIVER_PACKAGES[@]}" -eq 0 ]; then
    if gpu_processes_active; then
        log "Driver metadata is healthy; active workloads prevent the CUDA execution probe"
    else
        cuda_smoke || die "CUDA execution failed; diagnose before requesting --clean-driver"
    fi
    rm -f "$STATE_DIR/reboot-required"
    log "Matching Azure GRID driver is already usable; no driver changes made"
    exit 0
fi

if [ -r "$STATE_DIR/reboot-required" ]; then
    read -r previous_boot previous_version < "$STATE_DIR/reboot-required"
    if [ "$previous_boot" = "$BOOT_ID" ] && [ "$previous_version" = "$DRIVER_VERSION" ]; then
        log "A reboot is already pending. Reboot this VM, then rerun the same command."
        exit "$REBOOT_EXIT_CODE"
    fi
fi

require_quiescent_host
if [ "${#DRIVER_PACKAGES[@]}" -gt 0 ] && [ "$CLEAN_DRIVER" -eq 0 ]; then
    die "APT-managed drivers conflict with Azure GRID .run installation; rerun with --clean-driver after reviewing the package list"
fi
if [ "$CLEAN_DRIVER" -eq 0 ] && { command -v nvidia-uninstall >/dev/null 2>&1 || modinfo nvidia >/dev/null 2>&1; }; then
    die "a broken or different NVIDIA driver exists; use --clean-driver to replace it explicitly"
fi

apt-get update
apt-get install -y --no-install-recommends build-essential dkms "linux-headers-$KERNEL"
[ -f "/lib/modules/$KERNEL/build/Makefile" ] || die "running kernel headers are unavailable; reboot into a supported Ubuntu kernel first"

python3 - "$DRIVER_URL" "$DRIVER_RUN" <<'PY'
from urllib.parse import urlsplit
import sys
url = urlsplit(sys.argv[1])
if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
    raise SystemExit("Driver URL must be HTTPS without embedded credentials or fragments")
if url.path.rsplit("/", 1)[-1] != sys.argv[2]:
    raise SystemExit("Driver URL filename does not match --driver-version")
PY

INSTALLER="$CACHE_DIR/$DRIVER_RUN"
[ ! -L "$INSTALLER" ] || die "installer cache must not contain a symlink"
if [ ! -s "$INSTALLER" ]; then
    PARTIAL=$(mktemp "$CACHE_DIR/download.XXXXXX")
    trap 'if [ -n "${PARTIAL:-}" ]; then rm -f "$PARTIAL"; fi' EXIT
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 --location --fail --show-error \
        --retry 3 --connect-timeout 15 --max-time 900 --output "$PARTIAL" "$DRIVER_URL"
    [ "$(stat -c %s "$PARTIAL")" -ge $((100 * 1024 * 1024)) ] || die "driver download is unexpectedly small; refusing an HTML/error response"
    mv "$PARTIAL" "$INSTALLER"
    PARTIAL=""
fi
[ -f "$INSTALLER" ] && [ "$(stat -c %s "$INSTALLER")" -ge $((100 * 1024 * 1024)) ] || die "invalid installer cache; remove it and retry"
ACTUAL_SHA256=$(sha256sum "$INSTALLER" | awk '{print $1}')
log "Installer SHA-256: $ACTUAL_SHA256"
[ -z "$DRIVER_SHA256" ] || [ "$ACTUAL_SHA256" = "$DRIVER_SHA256" ] || die "installer SHA-256 mismatch"
sh "$INSTALLER" --check || die "NVIDIA installer integrity verification failed; remove the cached download and retry"

# Download and verify before removing anything, so a network failure cannot leave
# a working machine without its installer. This safety check is repeated because
# downloading can take long enough for a workload to start in the meantime.
require_quiescent_host

# Validate every cleanup operation before stopping persistence or touching the
# existing driver. A dependency/conflict error must leave that driver intact.
RUN_UNINSTALLER=""
NOUVEAU_CONF=/etc/modprobe.d/fabric-a10-nouveau.conf
[ ! -L "$NOUVEAU_CONF" ] || die "nouveau configuration must not be a symlink"
for required_tool in update-initramfs modprobe lsmod; do
    command -v "$required_tool" >/dev/null 2>&1 || die "missing required driver installation tool: $required_tool"
done
if [ "$CLEAN_DRIVER" -eq 1 ]; then
    if [ "${#DRIVER_PACKAGES[@]}" -gt 0 ]; then
        PURGE_PLAN=$(apt-get --simulate purge "${DRIVER_PACKAGES[@]}") \
            || die "APT cannot plan a clean driver removal; resolve package dependencies first"
        while read -r removed; do
            [ -z "$removed" ] || driver_package "$removed" \
                || die "APT would also remove unrelated package $removed; resolve dependencies manually"
        done < <(printf '%s\n' "$PURGE_PLAN" | awk '$1 == "Remv" || $1 == "Purg" { print $2 }')
    fi
    for uninstaller in /usr/bin/nvidia-uninstall /usr/local/bin/nvidia-uninstall; do
        if [ -x "$uninstaller" ]; then
            [ "$(stat -Lc %u "$uninstaller")" -eq 0 ] || die "NVIDIA uninstaller is not owned by root"
            RUN_UNINSTALLER=$uninstaller
            break
        fi
    done
    DKMS_REPORT=$(dkms status) || die "cannot inspect existing DKMS registrations"
    while read -r version; do
        [ -n "$version" ] || continue
        [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
            || die "unrecognized NVIDIA DKMS version $version; remove it manually"
    done < <(printf '%s\n' "$DKMS_REPORT" | awk -F'[,/]' '$1 == "nvidia" { print $2 }' | sort -u)
fi

# Persistence holds the control device but is not a model workload. Stop it only
# after all install gates and installer verification passed, before uninstalling.
if systemctl is-active --quiet nvidia-persistenced; then
    systemctl stop nvidia-persistenced
fi

if [ "$CLEAN_DRIVER" -eq 1 ]; then
    if [ -n "$RUN_UNINSTALLER" ]; then
        log "Removing the existing NVIDIA .run installation"
        "$RUN_UNINSTALLER" --silent || die "NVIDIA uninstall failed; inspect /var/log/nvidia-installer.log"
    fi
    if [ "${#DRIVER_PACKAGES[@]}" -gt 0 ]; then
        log "Purging only enumerated NVIDIA driver packages; no autoremove or broad package globs"
        apt-get purge -y "${DRIVER_PACKAGES[@]}"
    fi
    while read -r version; do
        [ -n "$version" ] || continue
        [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "unrecognized NVIDIA DKMS version $version; remove it manually"
        dkms remove -m nvidia -v "$version" --all
    done < <(dkms status | awk -F'[,/]' '$1 == "nvidia" { print $2 }' | sort -u)
fi

reboot_required() {
    printf '%s %s\n' "$BOOT_ID" "$DRIVER_VERSION" > "$STATE_DIR/reboot-required"
    log "$* Reboot this VM, then rerun the same command."
    exit "$REBOOT_EXIT_CODE"
}

printf 'blacklist nouveau\noptions nouveau modeset=0\n' > "$NOUVEAU_CONF"
update-initramfs -u -k "$KERNEL"
for module in nvidia_drm nvidia_modeset nvidia_uvm nvidia nouveau; do
    if lsmod | awk -v module="$module" '$1 == module { found=1 } END { exit !found }'; then
        modprobe -r "$module" || reboot_required "Cannot unload $module after cleanup."
    fi
done

log "Installing Azure GRID $DRIVER_VERSION with DKMS for $KERNEL"
sh "$INSTALLER" --silent --dkms --no-install-compat32-libs \
    || die "GRID installation failed; inspect $LOG_DIR and /var/log/nvidia-installer.log"
dkms status
update-initramfs -u -k "$KERNEL"
modprobe nvidia || reboot_required "GRID is installed but the kernel module cannot load yet."
modprobe nvidia_uvm || reboot_required "GRID is installed but the CUDA UVM module cannot load yet."
if ! driver_healthy; then
    reboot_required "GRID installed; nvidia-smi has not converged to the expected driver/devices."
fi
cuda_smoke || die "GRID installed but CUDA execution failed; inspect /var/log/nvidia-installer.log and kernel NVRM messages"
rm -f "$STATE_DIR/reboot-required"
log "Azure GRID $DRIVER_VERSION is usable on $EXPECTED_GPUS A10-24Q GPUs. Continue with k3s GPU setup."
