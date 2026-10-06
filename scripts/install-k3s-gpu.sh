#!/usr/bin/env bash
# Single-server k3s GPU setup. Run on the prepared Azure A10 VM, not your laptop
# or an AKS node. Driver cleanup/installation is a separate, explicit operation.
set -Eeuo pipefail

K3S_VERSION=v1.36.5+k3s1
TOOLKIT_VERSION=1.20.1-1
HELM_VERSION=v3.19.0
HELM_SHA256=a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496
EXPECTED_GPUS=2
NODE_NAME=""
KUBECONFIG_PATH=/etc/rancher/k3s/k3s.yaml
CONTAINERD_CONFIG=/var/lib/rancher/k3s/agent/etc/containerd/config.toml
LOG_DIR=/var/log/fabric/a10-host
TOOLKIT_PACKAGES=(nvidia-container-toolkit nvidia-container-toolkit-base libnvidia-container-tools libnvidia-container1)

usage() {
    cat <<'EOF'
Usage: sudo bash scripts/install-k3s-gpu.sh [--expected-gpus COUNT] [--node-name NAME]

Requires Ubuntu 22.04, working Azure GRID drivers, and A10-24Q devices on one VM.
Installs NVIDIA Container Toolkit 1.20.1-1, k3s v1.36.5+k3s1 (new servers only),
the NVIDIA device plugin, and Helm v3.19.0 when Helm is absent.
Labels measured GPU hardware, waits for allocatable GPUs, and checks GPU access
inside a short-lived CUDA 12.8 container. No models are deployed.

An existing local k3s server is reused without a version upgrade. If its NVIDIA
runtime is missing or its toolkit needs replacement, stop k3s yourself first.
The script never uses your current kubectl context, stops workloads, changes
AKS, rewrites containerd configuration, or installs generic NVIDIA drivers.
EOF
}
log() { printf '[fabric-k3s] %s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
while [ "$#" -gt 0 ]; do
    case "$1" in
        --expected-gpus|--node-name)
            [ "$#" -ge 2 ] && [ -n "$2" ] || die "$1 requires a value"
            case "$1" in --expected-gpus) EXPECTED_GPUS=$2 ;; --node-name) NODE_NAME=$2 ;; esac
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
[[ "$EXPECTED_GPUS" =~ ^[1-9][0-9]*$ ]] || die "--expected-gpus must be a positive integer"
[ "$(id -u)" -eq 0 ] || die "run this script with sudo on the A10 VM"
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 22.04 ] || die "requires Ubuntu 22.04"
[ "$(uname -m)" = x86_64 ] || die "requires x86_64"
for tool in curl python3 nvidia-smi flock timeout; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is missing; run prepare-a10-host.sh first"
done
[ -r /sys/class/dmi/id/sys_vendor ] || die "cannot identify Azure VM hardware"
case "$(cat /sys/class/dmi/id/sys_vendor)" in *Microsoft*) ;; *) die "requires an Azure VM" ;; esac
VM_SIZE=$(curl --noproxy '*' --fail --silent --show-error --connect-timeout 3 --max-time 10 \
    -H Metadata:true 'http://169.254.169.254/metadata/instance/compute/vmSize?api-version=2021-02-01&format=text') \
    || die "cannot verify the Azure VM SKU through IMDS"
[[ "$VM_SIZE" =~ ^Standard_NV[0-9]+ads_A10_v5$ ]] || die "requires NVadsA10_v5; found $VM_SIZE"
if systemctl is-active --quiet k3s-agent || systemctl cat k3s-agent.service >/dev/null 2>&1; then
    die "this host is a k3s worker; use the cluster's worker-join workflow instead"
fi

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PLUGIN_MANIFEST="$REPO_ROOT/deploy/cluster/nvidia-device-plugin-k3s.yaml"
[ -r "$PLUGIN_MANIFEST" ] || die "missing $PLUGIN_MANIFEST; copy the repository to this VM"
[ ! -L "$LOG_DIR" ] || die "log directory must not be a symlink"
install -d -m 0750 "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/k3s-$(date -u +%Y%m%dT%H%M%SZ)-$$.log") 2>&1
exec 9>/run/lock/fabric-a10-host.lock
flock -n 9 || die "another Fabric k3s setup is running"

GPU_REPORT=$(timeout 15 nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv,noheader,nounits) \
    || die "nvidia-smi failed; prepare the GRID driver before installing k3s"
GPU_MEMORY=$(printf '%s\n' "$GPU_REPORT" | python3 -c '
import csv, sys
rows = list(csv.reader(sys.stdin))
if len(rows) != int(sys.argv[1]):
    raise SystemExit("Unexpected usable GPU count")
memory = []
for row in rows:
    if len(row) != 3 or row[0].strip() != "NVIDIA A10-24Q" or row[2].strip() != "8.6":
        raise SystemExit("Expected full A10-24Q devices with compute capability 8.6")
    size = int(float(row[1].strip()))
    if size < 22000:
        raise SystemExit("Expected at least 22000 MiB per A10-24Q")
    memory.append(size)
print(min(memory))
' "$EXPECTED_GPUS") || die "GPU hardware does not match this setup"
log "Azure $VM_SIZE: $EXPECTED_GPUS A10-24Q devices, minimum ${GPU_MEMORY} MiB each"

K3S_EXISTS=0
K3S_ACTIVE=0
if systemctl cat k3s.service >/dev/null 2>&1; then K3S_EXISTS=1; fi
if systemctl is-active --quiet k3s; then K3S_ACTIVE=1; fi
if [ "$K3S_EXISTS" -eq 0 ] && { command -v k3s >/dev/null 2>&1 || [ -e "$KUBECONFIG_PATH" ]; }; then
    die "an unmanaged k3s installation exists; resolve it before creating a server"
fi
if [ "$K3S_EXISTS" -eq 0 ] && { [ -e /etc/rancher/k3s/config.yaml ] || [ -d /etc/rancher/k3s/config.yaml.d ]; }; then
    die "a preexisting k3s configuration could alter the new server; review and remove it yourself first"
fi
if [ "$K3S_EXISTS" -eq 0 ] && { [ -e /etc/default/k3s ] || [ -e /etc/sysconfig/k3s ]; }; then
    die "preexisting k3s service environment settings need review before installing a new server"
fi
if [ "$K3S_ACTIVE" -eq 1 ]; then
    [ -r "$CONTAINERD_CONFIG" ] && runtime_found=$(awk '/nvidia-container-runtime/ {found=1} END {print found+0}' "$CONTAINERD_CONFIG") \
        || die "existing k3s does not use the expected containerd configuration"
    [ "$runtime_found" -eq 1 ] || die "k3s must restart to discover NVIDIA; stop k3s yourself, then rerun"
fi

TOOLKIT_MISSING=0
for package in "${TOOLKIT_PACKAGES[@]}"; do
    if [ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)" != installed ]; then
        TOOLKIT_MISSING=1
    fi
done
if [ "$TOOLKIT_MISSING" -eq 1 ]; then
    [ "$K3S_ACTIVE" -eq 0 ] || die "stop k3s yourself before completing the NVIDIA Container Toolkit installation"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends ca-certificates curl gnupg
    WORK=$(mktemp -d)
    trap 'rm -rf "$WORK"' EXIT
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --silent --show-error \
        --location --connect-timeout 15 --max-time 120 \
        https://nvidia.github.io/libnvidia-container/gpgkey -o "$WORK/nvidia.asc"
    gpg --batch --yes --dearmor --output "$WORK/nvidia.gpg" "$WORK/nvidia.asc"
    [ ! -L /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg ] || die "toolkit keyring must not be a symlink"
    install -m 0644 "$WORK/nvidia.gpg" /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --silent --show-error \
        --location --connect-timeout 15 --max-time 120 \
        https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        -o "$WORK/nvidia.list"
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        "$WORK/nvidia.list" > "$WORK/nvidia-signed.list"
    [ ! -L /etc/apt/sources.list.d/nvidia-container-toolkit.list ] || die "toolkit source list must not be a symlink"
    install -m 0644 "$WORK/nvidia-signed.list" /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update
    PINNED_PACKAGES=()
    for package in "${TOOLKIT_PACKAGES[@]}"; do
        PINNED_PACKAGES+=("$package=$TOOLKIT_VERSION")
    done
    apt-get install -y --no-install-recommends "${PINNED_PACKAGES[@]}"
    rm -rf "$WORK"
    trap - EXIT
else
    log "Reusing installed NVIDIA Container Toolkit; no toolkit upgrade/downgrade"
fi
command -v nvidia-container-runtime >/dev/null 2>&1 || die "NVIDIA runtime executable is unavailable"

# k3s owns its generated containerd configuration and provides RuntimeClass nvidia.
# Installing the runtime before starting the server enables its native discovery.
if [ "$K3S_EXISTS" -eq 0 ]; then
    NODE_NAME=${NODE_NAME:-$(hostname -s | tr '[:upper:]_' '[:lower:]-')}
    [[ "$NODE_NAME" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] && [ "${#NODE_NAME}" -le 253 ] \
        || die "--node-name must be a Kubernetes DNS name"
    WORK=$(mktemp -d)
    trap 'rm -rf "$WORK"' EXIT
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --silent --show-error \
        --location --connect-timeout 15 --max-time 120 https://get.k3s.io -o "$WORK/install-k3s.sh"
    # Preserve the default Traefik ingress, ServiceLB, and local-path provisioner.
    # Inherited K3S_URL/TOKEN/EXEC settings must not silently join another cluster.
    env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        INSTALL_K3S_VERSION="$K3S_VERSION" sh "$WORK/install-k3s.sh" server \
        --node-name "$NODE_NAME" --write-kubeconfig-mode 600 --node-label accelerator=nvidia
    rm -rf "$WORK"
    trap - EXIT
elif [ "$K3S_ACTIVE" -eq 0 ]; then
    log "Starting the existing k3s server without changing its version or options"
    systemctl start k3s
else
    log "Reusing the running k3s server without restarting workloads"
fi

KUBECTL=(k3s kubectl --kubeconfig "$KUBECONFIG_PATH" --context default)
for attempt in $(seq 1 60); do
    if [ -r "$KUBECONFIG_PATH" ]; then
        API_SERVER=$("${KUBECTL[@]}" config view --minify -o jsonpath='{.clusters[0].cluster.server}')
        case "$API_SERVER" in
            https://127.0.0.1:6443|https://localhost:6443) ;;
            *) die "local k3s kubeconfig must target the loopback API on port 6443" ;;
        esac
        if "${KUBECTL[@]}" get nodes >/dev/null 2>&1; then break; fi
    fi
    [ "$attempt" -lt 60 ] || die "k3s API did not become available; inspect journalctl -u k3s"
    sleep 2
done
LOCAL_NAMES=$("${KUBECTL[@]}" get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
LOCAL_COUNT=$(printf '%s\n' "$LOCAL_NAMES" | awk 'NF {count++} END {print count+0}')
[ "$LOCAL_COUNT" -eq 1 ] || die "this installer requires a single-node cluster; found $LOCAL_COUNT nodes"
LOCAL_NODE=$(printf '%s\n' "$LOCAL_NAMES" | awk 'NF {print; exit}')
[ -z "$NODE_NAME" ] || [ "$NODE_NAME" = "$LOCAL_NODE" ] || die "existing node is $LOCAL_NODE, not $NODE_NAME"
"${KUBECTL[@]}" wait --for=condition=Ready "node/$LOCAL_NODE" --timeout=180s
[ -r "$CONTAINERD_CONFIG" ] && awk '/nvidia-container-runtime/ {found=1} END {exit !found}' "$CONTAINERD_CONFIG" \
    || die "k3s did not discover NVIDIA; inspect its PATH/runtime options before enrolling"
"${KUBECTL[@]}" get runtimeclass nvidia >/dev/null || die "k3s NVIDIA RuntimeClass is missing"
"${KUBECTL[@]}" label node "$LOCAL_NODE" --overwrite accelerator=nvidia \
    nvidia.com/gpu.product=NVIDIA-A10-24Q "nvidia.com/gpu.memory=$GPU_MEMORY" \
    nvidia.com/gpu.compute.major=8 nvidia.com/gpu.compute.minor=6 "nvidia.com/gpu.count=$EXPECTED_GPUS"
"${KUBECTL[@]}" annotate node "$LOCAL_NODE" --overwrite fabric.khushwant.dev/gpu-profile-source=host-nvidia-smi
"${KUBECTL[@]}" apply -f "$PLUGIN_MANIFEST"
"${KUBECTL[@]}" -n kube-system rollout status daemonset/nvidia-device-plugin-daemonset --timeout=180s
for attempt in $(seq 1 60); do
    ALLOCATABLE=$("${KUBECTL[@]}" get node "$LOCAL_NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}')
    [ "$ALLOCATABLE" = "$EXPECTED_GPUS" ] && break
    [ "$attempt" -lt 60 ] || die "allocatable GPUs are $ALLOCATABLE, expected $EXPECTED_GPUS; inspect device-plugin logs"
    sleep 2
done

# Do not queue a verification pod behind an existing model or displace its GPUs.
CLAIMED=$("${KUBECTL[@]}" get pods -A -o json | python3 -c '
import json, sys
total = 0
for pod in json.load(sys.stdin)["items"]:
    if pod.get("spec", {}).get("nodeName") != sys.argv[1] or pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
        continue
    spec = pod["spec"]
    def gpu(container):
        resources = container.get("resources", {})
        return int(resources.get("requests", {}).get("nvidia.com/gpu", resources.get("limits", {}).get("nvidia.com/gpu", 0)))
    # Conservative for restartable init containers: never assume their devices free.
    total += sum(gpu(c) for c in spec.get("containers", []) + spec.get("initContainers", []))
print(total)
' "$LOCAL_NODE")
if [ "$CLAIMED" -gt 0 ]; then
    log "$CLAIMED GPUs are claimed by existing pods; runtime verification deferred to preserve workloads"
else
    PROBE_NAME="fabric-gpu-check-$$"
    cleanup_probe() { "${KUBECTL[@]}" -n kube-system delete pod "$PROBE_NAME" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
    trap cleanup_probe EXIT
    cat <<EOF | "${KUBECTL[@]}" create -f -
apiVersion: v1
kind: Pod
metadata:
  name: $PROBE_NAME
  namespace: kube-system
spec:
  runtimeClassName: nvidia
  restartPolicy: Never
  activeDeadlineSeconds: 300
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchFields:
              - key: metadata.name
                operator: In
                values: [$LOCAL_NODE]
  tolerations:
    - operator: Exists
      effect: NoSchedule
  containers:
    - name: gpu-access
      # CUDA 12.8.1 base Ubuntu 22.04, public linux/amd64 image.
      image: nvidia/cuda@sha256:e869de8a68e95272e51c4c7071b1d39941aaab707f9bc051a99062a3791448b1
      command: ["sh", "-c"]
      args:
        - 'nvidia-smi -L && count=\$(nvidia-smi --query-gpu=index --format=csv,noheader | wc -l) && [ "\$count" -eq $EXPECTED_GPUS ]'
      resources:
        requests:
          nvidia.com/gpu: $EXPECTED_GPUS
        limits:
          nvidia.com/gpu: $EXPECTED_GPUS
EOF
    if ! "${KUBECTL[@]}" -n kube-system wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$PROBE_NAME" --timeout=300s; then
        "${KUBECTL[@]}" -n kube-system describe pod "$PROBE_NAME" || true
        "${KUBECTL[@]}" -n kube-system logs "$PROBE_NAME" || true
        die "GPU container failed; resolve its image/runtime/device error before enrolling"
    fi
    "${KUBECTL[@]}" -n kube-system logs "$PROBE_NAME"
    "${KUBECTL[@]}" -n kube-system delete pod "$PROBE_NAME" --wait=true --timeout=60s >/dev/null
    trap - EXIT
fi

if ! command -v helm >/dev/null 2>&1; then
    WORK=$(mktemp -d)
    trap 'rm -rf "$WORK"' EXIT
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --show-error \
        --location --connect-timeout 15 --max-time 300 \
        "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" -o "$WORK/helm.tar.gz"
    printf '%s  %s\n' "$HELM_SHA256" "$WORK/helm.tar.gz" | sha256sum --check --status \
        || die "Helm archive checksum mismatch"
    tar -xzf "$WORK/helm.tar.gz" -C "$WORK" linux-amd64/helm
    [ ! -L /usr/local/bin/helm ] || die "existing Helm path is a symlink; install Helm yourself"
    install -m 0755 "$WORK/linux-amd64/helm" /usr/local/bin/helm
    rm -rf "$WORK"
    trap - EXIT
fi
log "k3s is ready with $ALLOCATABLE allocatable GPUs on $LOCAL_NODE. Kubeconfig: $KUBECONFIG_PATH (root-only)."
log "Next: enroll this cluster with deploy/scripts/enroll-a10-stamp.sh, then deploy a model from the dashboard."
