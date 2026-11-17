#!/usr/bin/env bash
# Standalone VM setup: this file can be downloaded without cloning Fabric.
# Installs k3s/Helm and, on request, NVIDIA container support for existing drivers, the
# Kubernetes advertisement of a Cloud TPU VM's chips, or the Neuron device plugin on an AWS
# Inferentia2/Trainium instance.
set -Eeuo pipefail
umask 077

K3S_VERSION=v1.36.5+k3s1
HELM_VERSION=v3.19.0
TOOLKIT_VERSION=1.20.1-1
KUBECONFIG_PATH=/etc/rancher/k3s/k3s.yaml
CONTAINERD_CONFIG=/var/lib/rancher/k3s/agent/etc/containerd/config.toml
NODE_NAME=""
ENABLE_NVIDIA=0
TPU_TYPE=""
TPU_RESOURCE=google.com/tpu
TPU_ADVERTISER=/usr/local/sbin/fabric-tpu-advertise
TPU_UNIT=/etc/systemd/system/fabric-tpu-advertise.service
NEURON_TYPE=""
NEURON_RESOURCE=aws.amazon.com/neuron
# The official Neuron device plugin (aws-neuron-sdk src/k8, Neuron SDK 2.28 manifests).
NEURON_DEVICE_PLUGIN_IMAGE=public.ecr.aws/neuron/neuron-device-plugin:2.22.4.0
NEURON_ADVERTISER=/usr/local/sbin/fabric-neuron-advertise
NEURON_UNIT=/etc/systemd/system/fabric-neuron-advertise.service
WORK=""
TOOLKIT_PACKAGES=(nvidia-container-toolkit nvidia-container-toolkit-base libnvidia-container-tools libnvidia-container1)

usage() {
    cat <<'EOF'
Usage: sudo bash bootstrap-k3s.sh [--node-name NAME] [--nvidia | --tpu ACCELERATOR_TYPE | --neuron INSTANCE_TYPE]

Create a single-server k3s cluster on an Ubuntu 22.04/24.04 VM and install Helm
when absent. New servers use k3s v1.36.5+k3s1, Traefik, ServiceLB, local-path
storage, and the default runc runtime. Helm is pinned to v3.19.0.

--nvidia installs NVIDIA Container Toolkit 1.20.1-1 when absent, uses k3s NVIDIA
runtime discovery, labels measured GPUs, and installs device-plugin v0.17.1.
Working NVIDIA drivers and nvidia-smi are required beforehand. No GPU drivers,
Fabric stamp, control plane, or models are installed by this script.

--tpu ACCELERATOR_TYPE (for example v6e-4 or v5litepod-4) prepares a single-host
Cloud TPU VM: it counts the TPU device nodes (/dev/accel* or /dev/vfio/N), labels
the node accelerator=tpu with its accelerator type, and advertises the chips as
the google.com/tpu extended resource, re-applied by fabric-tpu-advertise.service
whenever k3s starts. No NVIDIA software, RuntimeClass or device plugin is used.

--neuron INSTANCE_TYPE (for example inf2.xlarge, trn1.32xlarge or trn2.48xlarge)
prepares an AWS Inferentia2/Trainium instance whose Neuron driver is installed (the
Neuron Deep Learning AMI ships it): it counts /dev/neuron*, labels the node
accelerator=neuron with its instance type, and installs the Neuron device plugin
(neuron-device-plugin 2.22.4.0), which advertises aws.amazon.com/neuron devices and
aws.amazon.com/neuroncore cores. If the plugin cannot start, the devices are advertised
as aws.amazon.com/neuron instead, re-applied by fabric-neuron-advertise.service.

An existing single-node local k3s server is reused without changing its version,
service settings, or containerd configuration. If NVIDIA support needs a server
restart, stop k3s yourself first and rerun. This script never stops workloads or
uses your current kubectl context. The local kubeconfig remains root-only.
EOF
}
log() { printf '[fabric-k3s] %s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
cleanup() { if [ -n "$WORK" ]; then rm -rf "$WORK"; fi; }
download() {
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --fail --silent --show-error --location --retry 3 \
        --connect-timeout 15 --max-time 300 "$1" -o "$2"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --node-name)
            [ "$#" -ge 2 ] && [ -n "$2" ] || die '--node-name requires a value'
            NODE_NAME=$2; shift 2 ;;
        --nvidia) ENABLE_NVIDIA=1; shift ;;
        --tpu)
            [ "$#" -ge 2 ] && [ -n "$2" ] || die '--tpu requires the accelerator type, e.g. v6e-4'
            TPU_TYPE=$2; shift 2 ;;
        --neuron)
            [ "$#" -ge 2 ] && [ -n "$2" ] || die '--neuron requires the instance type, e.g. inf2.xlarge'
            NEURON_TYPE=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
if [ -n "$NODE_NAME" ]; then
    [[ "$NODE_NAME" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] && [ "${#NODE_NAME}" -le 63 ] \
        || die '--node-name must be a lowercase DNS label, at most 63 characters'
fi
if [ -n "$TPU_TYPE" ]; then
    [ "$ENABLE_NVIDIA" -eq 0 ] || die '--nvidia and --tpu are mutually exclusive'
    [[ "$TPU_TYPE" =~ ^(v4|v5p|v5e|v5litepod|v6e)-[0-9]+$ ]] \
        || die "--tpu must be a supported accelerator type such as v6e-4, v5litepod-4, v5p-8 or v4-8 (got $TPU_TYPE)"
fi
if [ -n "$NEURON_TYPE" ]; then
    [ "$ENABLE_NVIDIA" -eq 0 ] || die '--nvidia and --neuron are mutually exclusive'
    [ -z "$TPU_TYPE" ] || die '--tpu and --neuron are mutually exclusive'
    [[ "$NEURON_TYPE" =~ ^(inf2|trn1|trn1n|trn2|trn2u)\.[a-z0-9]+$ ]] \
        || die "--neuron must be an Inferentia2 or Trainium instance type such as inf2.xlarge, trn1.32xlarge or trn2.48xlarge (got $NEURON_TYPE)"
fi
[ "$(id -u)" -eq 0 ] || die 'run with sudo on the VM that will host k3s'
[ "$(uname -s)" = Linux ] || die 'requires Linux'
[ -r /etc/os-release ] || die 'cannot identify the operating system'
. /etc/os-release
[ "${ID:-}" = ubuntu ] || die 'requires Ubuntu 22.04 or 24.04'
case "${VERSION_ID:-}" in 22.04|24.04) ;; *) die 'requires Ubuntu 22.04 or 24.04' ;; esac
case "$(uname -m)" in
    x86_64)
        ARCH=amd64
        HELM_SHA256=a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496 ;;
    aarch64|arm64)
        ARCH=arm64
        HELM_SHA256=440cf7add0aee27ebc93fada965523c1dc2e0ab340d4348da2215737fc0d76ad ;;
    *) die 'requires x86_64 or aarch64' ;;
esac
command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] \
    || die 'requires a VM with systemd running'
for tool in flock timeout apt-get dpkg-query awk; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done
if systemctl is-active --quiet k3s-agent || systemctl cat k3s-agent.service >/dev/null 2>&1; then
    die 'this VM is a k3s worker; use its existing cluster join workflow'
fi

# Share the driver/setup lock with Fabric's A10 scripts: k3s cannot start while
# another Fabric process is removing the host's NVIDIA driver.
[ ! -L /run/lock/fabric-a10-host.lock ] || die 'setup lock must not be a symlink'
exec 9>/run/lock/fabric-a10-host.lock
flock -n 9 || die 'another Fabric host setup is running'
trap cleanup EXIT

K3S_EXISTS=0
K3S_ACTIVE=0
if systemctl cat k3s.service >/dev/null 2>&1; then K3S_EXISTS=1; fi
if systemctl is-active --quiet k3s; then K3S_ACTIVE=1; fi
if [ "$K3S_EXISTS" -eq 0 ]; then
    if command -v k3s >/dev/null 2>&1 || [ -e "$KUBECONFIG_PATH" ]; then
        die 'an unmanaged k3s installation exists; resolve it before creating a server'
    fi
    for path in /etc/rancher/k3s/config.yaml /etc/rancher/k3s/config.yaml.d \
        /etc/default/k3s /etc/sysconfig/k3s /etc/systemd/system/k3s.service.env \
        /var/lib/rancher/k3s; do
        [ ! -e "$path" ] && [ ! -L "$path" ] \
            || die "preexisting $path could alter the new server; review it yourself first"
    done
else
    command -v k3s >/dev/null 2>&1 || die 'existing k3s service has no k3s executable on PATH'
fi

if [ "$ENABLE_NVIDIA" -eq 1 ]; then
    command -v nvidia-smi >/dev/null 2>&1 \
        || die '--nvidia requires existing working drivers and nvidia-smi; install the correct host driver first'
    GPU_REPORT=$(timeout 20s nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv,noheader,nounits) \
        || die 'nvidia-smi failed; repair the host driver before installing Kubernetes GPU support'
    [ -n "$GPU_REPORT" ] || die 'nvidia-smi reports no GPUs'
    if [ "$K3S_ACTIVE" -eq 1 ]; then
        [ -r "$CONTAINERD_CONFIG" ] && awk '/nvidia-container-runtime/ {found=1} END {exit !found}' "$CONTAINERD_CONFIG" \
            || die 'running k3s has not discovered NVIDIA; stop k3s yourself, then rerun with --nvidia'
    fi
    # Completing a partial installation must not upgrade/downgrade packages that
    # were already installed for an existing host or runtime.
    TOOLKIT_MISSING=0
    TOOLKIT_PRESENT=0
    TOOLKIT_VERSION_CONFLICT=0
    for package in "${TOOLKIT_PACKAGES[@]}"; do
        package_status=$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)
        if [ "$package_status" = installed ]; then
            TOOLKIT_PRESENT=$((TOOLKIT_PRESENT + 1))
            package_version=$(dpkg-query -W -f='${Version}' "$package")
            if [ "$package_version" != "$TOOLKIT_VERSION" ]; then TOOLKIT_VERSION_CONFLICT=1; fi
        elif [ -n "$package_status" ] && [ "$package_status" != not-installed ] && [ "$package_status" != config-files ]; then
            die "$package is $package_status; repair the partial package transaction yourself first"
        else
            TOOLKIT_MISSING=1
        fi
    done
    if [ "$TOOLKIT_MISSING" -eq 1 ] && [ "$TOOLKIT_PRESENT" -gt 0 ] && [ "$TOOLKIT_VERSION_CONFLICT" -eq 1 ]; then
        die "partial NVIDIA Container Toolkit uses a version other than $TOOLKIT_VERSION; reconcile its package versions yourself first"
    fi
    if [ "$TOOLKIT_MISSING" -eq 1 ] && [ "$K3S_ACTIVE" -eq 1 ]; then
        die 'stop k3s yourself before completing the NVIDIA Container Toolkit installation'
    fi
fi

if [ -n "$TPU_TYPE" ]; then
    # v4 and v5p accelerator types count TensorCores, two per chip; v5e and v6e count chips.
    TPU_SIZE=${TPU_TYPE##*-}
    case "$TPU_TYPE" in v4-*|v5p-*) TPU_EXPECTED=$(( TPU_SIZE / 2 )) ;; *) TPU_EXPECTED=$TPU_SIZE ;; esac
    [ "$TPU_EXPECTED" -ge 1 ] || TPU_EXPECTED=1
    TPU_CHIPS=$( { ls -1d /dev/accel[0-9]* 2>/dev/null || true; ls -1 /dev/vfio 2>/dev/null | grep -E '^[0-9]+$' || true; } | awk 'NF {count++} END {print count+0}')
    [ "$TPU_CHIPS" -gt 0 ] \
        || die "no TPU device nodes (/dev/accel* or /dev/vfio/N) on this VM; is it a Cloud TPU VM ($TPU_TYPE)?"
    if [ "$TPU_EXPECTED" -gt "$TPU_CHIPS" ]; then
        die "$TPU_TYPE has $TPU_EXPECTED chips but this host sees $TPU_CHIPS: multi-host TPU slices are not supported; use a single-host type such as v6e-4 or v5litepod-4"
    fi
    log "$TPU_CHIPS TPU chips found for $TPU_TYPE"
fi

if [ -n "$NEURON_TYPE" ]; then
    NEURON_NODES=$(ls -1d /dev/neuron[0-9]* 2>/dev/null || true)
    NEURON_COUNT=$(printf '%s\n' "$NEURON_NODES" | awk 'NF {count++} END {print count+0}')
    [ "$NEURON_COUNT" -gt 0 ] \
        || die "no Neuron devices (/dev/neuron*) on this instance; install the Neuron driver (aws-neuronx-dkms) or use the Neuron Deep Learning AMI ($NEURON_TYPE)"
    log "$NEURON_COUNT Neuron devices found for $NEURON_TYPE"
fi

export DEBIAN_FRONTEND=noninteractive
PREREQUISITES=(ca-certificates curl python3)
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
WORK=$(mktemp -d)

if [ "$ENABLE_NVIDIA" -eq 1 ]; then
    GPU_PROFILE=$(printf '%s\n' "$GPU_REPORT" | python3 -c '
import csv, re, sys
from decimal import Decimal, InvalidOperation
rows = list(csv.reader(sys.stdin))
if not rows:
    raise SystemExit("No GPU devices reported")
products, memories, capabilities = set(), [], []
for row in rows:
    if len(row) != 3 or not row[0].strip():
        raise SystemExit("Unrecognized nvidia-smi hardware report")
    products.add(row[0].strip())
    try:
        memory = Decimal(row[1].strip())
    except InvalidOperation:
        raise SystemExit("GPU memory report is not numeric")
    capability = re.fullmatch(r"([0-9]+)\.([0-9]+)", row[2].strip())
    if not memory.is_finite() or memory <= 0 or not capability:
        raise SystemExit("GPU memory/compute capability is unavailable")
    memories.append(int(memory))
    capabilities.append((int(capability[1]), int(capability[2])))
product = ""
if len(products) == 1:
    product = re.sub(r"[^A-Za-z0-9_.-]", "-", next(iter(products))).strip("-_.")
    if not product or len(product) > 63:
        raise SystemExit("GPU product cannot be represented as a Kubernetes label")
major, minor = min(capabilities)
print(len(rows), min(memories), major, minor, product or "mixed")
' ) || die 'cannot determine truthful GPU labels from nvidia-smi'
    read -r GPU_COUNT GPU_MEMORY GPU_MAJOR GPU_MINOR GPU_PRODUCT <<< "$GPU_PROFILE"
    log "$GPU_COUNT GPUs measured: minimum ${GPU_MEMORY} MiB, minimum compute ${GPU_MAJOR}.${GPU_MINOR}, product $GPU_PRODUCT"

    if [ "$TOOLKIT_MISSING" -eq 1 ]; then
        apt-get update
        apt-get install -y --no-install-recommends gnupg
        download https://nvidia.github.io/libnvidia-container/gpgkey "$WORK/nvidia.asc"
        gpg --batch --yes --dearmor --output "$WORK/nvidia.gpg" "$WORK/nvidia.asc"
        [ ! -L /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg ] \
            || die 'NVIDIA toolkit keyring must not be a symlink'
        install -m 0644 "$WORK/nvidia.gpg" /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
        download https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list "$WORK/nvidia.list"
        sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
            "$WORK/nvidia.list" > "$WORK/nvidia-signed.list"
        [ ! -L /etc/apt/sources.list.d/nvidia-container-toolkit.list ] \
            || die 'NVIDIA toolkit source list must not be a symlink'
        install -m 0644 "$WORK/nvidia-signed.list" /etc/apt/sources.list.d/nvidia-container-toolkit.list
        apt-get update
        PINNED_PACKAGES=()
        for package in "${TOOLKIT_PACKAGES[@]}"; do PINNED_PACKAGES+=("$package=$TOOLKIT_VERSION"); done
        apt-get install -y --no-install-recommends "${PINNED_PACKAGES[@]}"
    else
        log 'Reusing installed NVIDIA Container Toolkit without an upgrade or downgrade'
    fi
    command -v nvidia-container-runtime >/dev/null 2>&1 \
        || die 'NVIDIA container runtime executable is unavailable'
fi

if [ "$K3S_EXISTS" -eq 0 ]; then
    NODE_NAME=${NODE_NAME:-$(hostname -s | tr '[:upper:]_' '[:lower:]-')}
    [[ "$NODE_NAME" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] && [ "${#NODE_NAME}" -le 63 ] \
        || die 'hostname is not a DNS label; provide --node-name'
    download https://get.k3s.io "$WORK/install-k3s.sh"
    # Do not inherit K3S_URL/TOKEN/EXEC or caller kubeconfig/cluster settings.
    # k3s discovers nvidia-container-runtime on its own PATH before containerd starts.
    env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        INSTALL_K3S_VERSION="$K3S_VERSION" sh "$WORK/install-k3s.sh" server \
        --node-name "$NODE_NAME" --write-kubeconfig-mode 600
elif [ "$K3S_ACTIVE" -eq 0 ]; then
    log 'Starting the existing k3s server without changing its version or service options'
    systemctl start k3s
else
    log 'Reusing running k3s without restarting workloads'
fi

KUBECTL=(k3s kubectl --kubeconfig "$KUBECONFIG_PATH" --context default)
for attempt in $(seq 1 60); do
    if [ -r "$KUBECONFIG_PATH" ]; then
        [ ! -L "$KUBECONFIG_PATH" ] || die 'local k3s kubeconfig must not be a symlink'
        [ "$(stat -Lc %u "$KUBECONFIG_PATH")" -eq 0 ] || die 'local k3s kubeconfig must be owned by root'
        KUBECONFIG_MODE=$(stat -Lc %a "$KUBECONFIG_PATH")
        (( (8#$KUBECONFIG_MODE & 022) == 0 )) \
            || die 'local k3s kubeconfig is group/other writable; correct its ownership and mode yourself first'
        chmod 0600 "$KUBECONFIG_PATH"
        API_SERVER=$("${KUBECTL[@]}" config view --minify -o jsonpath='{.clusters[0].cluster.server}')
        case "$API_SERVER" in
            https://127.0.0.1:6443|https://localhost:6443|https://\[::1\]:6443) ;;
            *) die 'local k3s kubeconfig must target its loopback API on port 6443' ;;
        esac
        # The API answers before a fresh server has registered its own node, so wait for
        # the node itself; counting too early reported "found 0 nodes" on a new VM.
        if [ -n "$("${KUBECTL[@]}" get nodes -o name 2>/dev/null)" ]; then break; fi
    fi
    [ "$attempt" -lt 60 ] || die 'k3s did not register its node; inspect journalctl -u k3s'
    sleep 2
done
LOCAL_NAMES=$("${KUBECTL[@]}" get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
LOCAL_COUNT=$(printf '%s\n' "$LOCAL_NAMES" | awk 'NF {count++} END {print count+0}')
[ "$LOCAL_COUNT" -eq 1 ] || die "requires a single-node cluster; found $LOCAL_COUNT nodes"
LOCAL_NODE=$(printf '%s\n' "$LOCAL_NAMES" | awk 'NF {print; exit}')
[ -z "$NODE_NAME" ] || [ "$NODE_NAME" = "$LOCAL_NODE" ] || die "existing node is $LOCAL_NODE, not $NODE_NAME"
"${KUBECTL[@]}" wait --for=condition=Ready "node/$LOCAL_NODE" --timeout=180s

if [ "$ENABLE_NVIDIA" -eq 1 ]; then
    [ -r "$CONTAINERD_CONFIG" ] && awk '/nvidia-container-runtime/ {found=1} END {exit !found}' "$CONTAINERD_CONFIG" \
        || die 'k3s did not discover NVIDIA; inspect the service PATH and runtime options'
    # k3s creates the nvidia RuntimeClass from its packaged manifests a few seconds after the
    # node is Ready, so a fresh server needs a short wait before it exists.
    for attempt in $(seq 1 60); do
        "${KUBECTL[@]}" get runtimeclass nvidia >/dev/null 2>&1 && break
        [ "$attempt" -lt 60 ] || die 'k3s NVIDIA RuntimeClass did not appear within 2 minutes; inspect journalctl -u k3s'
        sleep 2
    done
    LABELS=(accelerator=nvidia "nvidia.com/gpu.memory=$GPU_MEMORY" \
        "nvidia.com/gpu.compute.major=$GPU_MAJOR" "nvidia.com/gpu.compute.minor=$GPU_MINOR" \
        "nvidia.com/gpu.count=$GPU_COUNT")
    if [ "$GPU_PRODUCT" != mixed ]; then
        LABELS+=("nvidia.com/gpu.product=$GPU_PRODUCT")
    else
        log 'Mixed GPU products: no homogeneous product label will be added'
    fi
    NODE_JSON=$("${KUBECTL[@]}" get node "$LOCAL_NODE" -o json)
    # Hardware labels from another profiler are retained when they agree. Conflicts
    # require explicit review rather than silently overwriting NFD/GPU Operator data.
    printf '%s\n' "$NODE_JSON" | python3 -c '
import json, sys
labels = json.load(sys.stdin).get("metadata", {}).get("labels", {})
for pair in sys.argv[1:]:
    key, value = pair.split("=", 1)
    if key in labels and labels[key] != value:
        raise SystemExit(f"Existing label {key}={labels[key]} disagrees with measured value {value}; review it first")
' "${LABELS[@]}" || die 'existing node labels conflict with measured hardware'
    if [ "$GPU_PRODUCT" = mixed ]; then
        PRODUCT_LABEL=$("${KUBECTL[@]}" get node "$LOCAL_NODE" -o jsonpath='{.metadata.labels.nvidia\.com/gpu\.product}')
        [ -z "$PRODUCT_LABEL" ] || die 'mixed GPUs have an existing product label; review it before proceeding'
    fi
    "${KUBECTL[@]}" label node "$LOCAL_NODE" "${LABELS[@]}"
    "${KUBECTL[@]}" annotate node "$LOCAL_NODE" --overwrite fabric.khushwant.dev/gpu-profile-source=host-nvidia-smi

    EXISTING_PLUGIN=$("${KUBECTL[@]}" -n kube-system get daemonset nvidia-device-plugin-daemonset \
        --ignore-not-found -o jsonpath='{.metadata.labels.app\.kubernetes\.io/part-of}')
    PLUGIN_PRESENT=$("${KUBECTL[@]}" -n kube-system get daemonset nvidia-device-plugin-daemonset \
        --ignore-not-found -o name)
    CURRENT_ALLOCATABLE=$("${KUBECTL[@]}" get node "$LOCAL_NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}')
    PROVIDER_WORKLOADS=$("${KUBECTL[@]}" get daemonsets,deployments -A -o json)
    OTHER_PROVIDERS=$(printf '%s\n' "$PROVIDER_WORKLOADS" | python3 -c '
import json, sys
providers = []
for item in json.load(sys.stdin).get("items", []):
    metadata = item.get("metadata", {})
    name, namespace = metadata.get("name", ""), metadata.get("namespace", "")
    if item.get("kind") == "DaemonSet" and namespace == "kube-system" and name == "nvidia-device-plugin-daemonset":
        continue
    pod = item.get("spec", {}).get("template", {})
    labels = {**metadata.get("labels", {}), **pod.get("metadata", {}).get("labels", {})}
    values = " ".join([name, *(str(value) for value in labels.values())]).lower().replace("_", "-")
    containers = pod.get("spec", {}).get("containers", []) + pod.get("spec", {}).get("initContainers", [])
    images = [str(container.get("image", "")).lower() for container in containers]
    recognized = (
        any(token in values for token in ("nvidia-device-plugin", "gpu-operator"))
        or ("nvidia" in values and "device-plugin" in values)
        or any("k8s-device-plugin" in image or "gpu-operator" in image
               or ("nvidia" in image and "device-plugin" in image) for image in images)
    )
    if recognized:
        providers.append(f"{namespace}/{name}")
print(", ".join(providers))
')
    WAIT_FOR_CANONICAL_PLUGIN=0
    if [ -n "$OTHER_PROVIDERS" ]; then
        log "Existing NVIDIA provider workloads: $OTHER_PROVIDERS; no device plugin installed or changed"
    elif [ -z "$PLUGIN_PRESENT" ] && [ -n "$CURRENT_ALLOCATABLE" ] && [ "$CURRENT_ALLOCATABLE" != 0 ]; then
        log 'Reusing the existing GPU resource provider; no second device plugin installed'
    elif [ -n "$PLUGIN_PRESENT" ] && [ "$EXISTING_PLUGIN" != fabric ]; then
        log 'Reusing the existing NVIDIA device-plugin DaemonSet without replacing it'
        WAIT_FOR_CANONICAL_PLUGIN=1
    else
        cat <<'EOF' | "${KUBECTL[@]}" apply -f -
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: nvidia-device-plugin-daemonset
  namespace: kube-system
  labels:
    app.kubernetes.io/name: nvidia-device-plugin
    app.kubernetes.io/part-of: fabric
spec:
  selector:
    matchLabels:
      name: nvidia-device-plugin-ds
  updateStrategy:
    type: RollingUpdate
  template:
    metadata:
      labels:
        name: nvidia-device-plugin-ds
    spec:
      runtimeClassName: nvidia
      priorityClassName: system-node-critical
      nodeSelector:
        accelerator: nvidia
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
        - key: sku
          operator: Equal
          value: gpu
          effect: NoSchedule
        - key: node-role.kubernetes.io/control-plane
          operator: Exists
          effect: NoSchedule
      containers:
        - name: nvidia-device-plugin-ctr
          image: nvcr.io/nvidia/k8s-device-plugin:v0.17.1
          env:
            - name: FAIL_ON_INIT_ERROR
              value: "true"
            - name: NVIDIA_VISIBLE_DEVICES
              value: all
            - name: NVIDIA_DRIVER_CAPABILITIES
              value: compute,utility
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: [ALL]
          volumeMounts:
            - name: device-plugin
              mountPath: /var/lib/kubelet/device-plugins
      volumes:
        - name: device-plugin
          hostPath:
            path: /var/lib/kubelet/device-plugins
            type: Directory
EOF
        WAIT_FOR_CANONICAL_PLUGIN=1
    fi
    if [ "$WAIT_FOR_CANONICAL_PLUGIN" -eq 1 ]; then
        "${KUBECTL[@]}" -n kube-system rollout status daemonset/nvidia-device-plugin-daemonset --timeout=180s
    fi
    for attempt in $(seq 1 60); do
        ALLOCATABLE=$("${KUBECTL[@]}" get node "$LOCAL_NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}')
        [ "$ALLOCATABLE" = "$GPU_COUNT" ] && break
        [ "$attempt" -lt 60 ] \
            || die "allocatable GPUs are ${ALLOCATABLE:-0}, measured $GPU_COUNT; inspect device-plugin/MIG/sharing configuration"
        sleep 2
    done
    log "$ALLOCATABLE GPUs are allocatable. Model pods must request nvidia.com/gpu and RuntimeClass nvidia."
fi

if [ -n "$TPU_TYPE" ]; then
    LABELS=(accelerator=tpu "fabric.khushwant.dev/tpu-accelerator-type=$TPU_TYPE")
    EXISTING_ACCELERATOR=$("${KUBECTL[@]}" get node "$LOCAL_NODE" -o jsonpath='{.metadata.labels.accelerator}')
    [ -z "$EXISTING_ACCELERATOR" ] || [ "$EXISTING_ACCELERATOR" = tpu ] \
        || die "node $LOCAL_NODE is labelled accelerator=$EXISTING_ACCELERATOR; review it before preparing it as a TPU node"
    "${KUBECTL[@]}" label node "$LOCAL_NODE" --overwrite "${LABELS[@]}"

    # No device plugin exists for a TPU VM, so the chips are advertised as an extended
    # resource on the node's status. The kubelet keeps it across restarts, but a node that is
    # re-registered loses it, so a oneshot unit applies it again every time k3s starts.
    PATCH="{\"status\":{\"capacity\":{\"$TPU_RESOURCE\":\"$TPU_CHIPS\"}}}"
    K3S_BIN=$(command -v k3s)
    [ ! -L "$TPU_ADVERTISER" ] && [ ! -L "$TPU_UNIT" ] || die 'the TPU advertisement files must not be symlinks'
    cat > "$WORK/fabric-tpu-advertise" <<EOF
#!/bin/sh
# Advertises this Cloud TPU VM's chips to k3s. Written by Fabric's bootstrap-k3s.sh --tpu.
set -eu
for attempt in \$(seq 1 90); do
    if $K3S_BIN kubectl --kubeconfig $KUBECONFIG_PATH patch node $LOCAL_NODE --subresource=status --type=merge -p '$PATCH' >/dev/null 2>&1; then
        exit 0
    fi
    sleep 2
done
echo "could not advertise $TPU_CHIPS $TPU_RESOURCE on node $LOCAL_NODE" >&2
exit 1
EOF
    install -m 0755 "$WORK/fabric-tpu-advertise" "$TPU_ADVERTISER"
    cat > "$WORK/fabric-tpu-advertise.service" <<EOF
[Unit]
Description=Advertise Cloud TPU chips to k3s (Fabric)
After=k3s.service
PartOf=k3s.service

[Service]
Type=oneshot
ExecStart=$TPU_ADVERTISER

[Install]
WantedBy=k3s.service
EOF
    install -m 0644 "$WORK/fabric-tpu-advertise.service" "$TPU_UNIT"
    systemctl daemon-reload
    systemctl enable fabric-tpu-advertise.service
    "$TPU_ADVERTISER" || die "could not advertise $TPU_RESOURCE on node $LOCAL_NODE; inspect journalctl -u k3s"
    for attempt in $(seq 1 60); do
        ALLOCATABLE=$("${KUBECTL[@]}" get node "$LOCAL_NODE" -o jsonpath='{.status.allocatable.google\.com/tpu}')
        [ "$ALLOCATABLE" = "$TPU_CHIPS" ] && break
        [ "$attempt" -lt 60 ] || die "allocatable TPU chips are ${ALLOCATABLE:-0}, found $TPU_CHIPS; inspect journalctl -u k3s"
        sleep 2
    done
    log "$ALLOCATABLE TPU chips are allocatable. Model pods must request $TPU_RESOURCE and run privileged with the TPU devices."
fi

if [ -n "$NEURON_TYPE" ]; then
    LABELS=(accelerator=neuron "fabric.khushwant.dev/neuron-instance-type=$NEURON_TYPE")
    EXISTING_ACCELERATOR=$("${KUBECTL[@]}" get node "$LOCAL_NODE" -o jsonpath='{.metadata.labels.accelerator}')
    [ -z "$EXISTING_ACCELERATOR" ] || [ "$EXISTING_ACCELERATOR" = neuron ] \
        || die "node $LOCAL_NODE is labelled accelerator=$EXISTING_ACCELERATOR; review it before preparing it as a Neuron node"
    "${KUBECTL[@]}" label node "$LOCAL_NODE" --overwrite "${LABELS[@]}"

    # The official Neuron device plugin, as AWS publishes it, except that it is selected by
    # Fabric's node label: the upstream manifest selects nodes by their EC2 instance-type
    # label, which k3s sets to "k3s", and reads a kubelet kubeconfig path k3s does not have,
    # so it uses its ServiceAccount instead.
    "${KUBECTL[@]}" apply -f - <<EOF
kind: ClusterRole
apiVersion: rbac.authorization.k8s.io/v1
metadata:
  name: neuron-device-plugin
rules:
  - apiGroups: [""]
    resources: [nodes]
    verbs: [get, list, watch]
  - apiGroups: [""]
    resources: [events]
    verbs: [create, patch]
  - apiGroups: [""]
    resources: [pods]
    verbs: [update, patch, get, list, watch]
  - apiGroups: [""]
    resources: [nodes/status]
    verbs: [patch, update]
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: neuron-device-plugin
  namespace: kube-system
---
kind: ClusterRoleBinding
apiVersion: rbac.authorization.k8s.io/v1
metadata:
  name: neuron-device-plugin
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: neuron-device-plugin
subjects:
  - kind: ServiceAccount
    name: neuron-device-plugin
    namespace: kube-system
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: neuron-device-plugin-daemonset
  namespace: kube-system
spec:
  selector:
    matchLabels:
      name: neuron-device-plugin-ds
  updateStrategy:
    type: RollingUpdate
  template:
    metadata:
      labels:
        name: neuron-device-plugin-ds
    spec:
      serviceAccountName: neuron-device-plugin
      priorityClassName: system-node-critical
      nodeSelector:
        accelerator: neuron
      tolerations:
        - key: CriticalAddonsOnly
          operator: Exists
        - key: aws.amazon.com/neuron
          operator: Exists
          effect: NoSchedule
        - key: node-role.kubernetes.io/control-plane
          operator: Exists
          effect: NoSchedule
      containers:
        - name: neuron-device-plugin
          image: $NEURON_DEVICE_PLUGIN_IMAGE
          imagePullPolicy: IfNotPresent
          env:
            - name: NODE_NAME
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: [ALL]
          volumeMounts:
            - name: device-plugin
              mountPath: /var/lib/kubelet/device-plugins
            - name: infa-map
              mountPath: /run
      volumes:
        - name: device-plugin
          hostPath:
            path: /var/lib/kubelet/device-plugins
        - name: infa-map
          hostPath:
            path: /run
EOF
    NEURON_PLUGIN_OK=0
    if "${KUBECTL[@]}" -n kube-system rollout status daemonset/neuron-device-plugin-daemonset --timeout=300s; then
        for attempt in $(seq 1 60); do
            ALLOCATABLE=$("${KUBECTL[@]}" get node "$LOCAL_NODE" -o jsonpath='{.status.allocatable.aws\.amazon\.com/neuron}')
            if [ "$ALLOCATABLE" = "$NEURON_COUNT" ]; then NEURON_PLUGIN_OK=1; break; fi
            sleep 2
        done
    fi
    if [ "$NEURON_PLUGIN_OK" -eq 1 ]; then
        # A previous fallback is no longer needed once the plugin owns the resource.
        if [ -f "$NEURON_UNIT" ]; then
            systemctl disable fabric-neuron-advertise.service >/dev/null 2>&1 || true
            rm -f "$NEURON_UNIT" "$NEURON_ADVERTISER"
            systemctl daemon-reload
        fi
        log "$ALLOCATABLE Neuron devices are allocatable through the Neuron device plugin. Model pods must request $NEURON_RESOURCE."
    else
        # Fallback, as on a TPU VM: advertise the devices on the node's status and re-apply it
        # whenever k3s starts. Nothing injects the devices then, so model pods must run
        # privileged with /dev/neuron* mounted.
        log "WARNING: the Neuron device plugin did not advertise $NEURON_COUNT devices; advertising $NEURON_RESOURCE directly"
        "${KUBECTL[@]}" -n kube-system get pods -l name=neuron-device-plugin-ds -o wide || true
        PATCH="{\"status\":{\"capacity\":{\"$NEURON_RESOURCE\":\"$NEURON_COUNT\"}}}"
        K3S_BIN=$(command -v k3s)
        [ ! -L "$NEURON_ADVERTISER" ] && [ ! -L "$NEURON_UNIT" ] || die 'the Neuron advertisement files must not be symlinks'
        cat > "$WORK/fabric-neuron-advertise" <<EOF
#!/bin/sh
# Advertises this instance's Neuron devices to k3s. Written by Fabric's bootstrap-k3s.sh --neuron
# because the Neuron device plugin could not run.
set -eu
for attempt in \$(seq 1 90); do
    if $K3S_BIN kubectl --kubeconfig $KUBECONFIG_PATH patch node $LOCAL_NODE --subresource=status --type=merge -p '$PATCH' >/dev/null 2>&1; then
        exit 0
    fi
    sleep 2
done
echo "could not advertise $NEURON_COUNT $NEURON_RESOURCE on node $LOCAL_NODE" >&2
exit 1
EOF
        install -m 0755 "$WORK/fabric-neuron-advertise" "$NEURON_ADVERTISER"
        cat > "$WORK/fabric-neuron-advertise.service" <<EOF
[Unit]
Description=Advertise AWS Neuron devices to k3s (Fabric)
After=k3s.service
PartOf=k3s.service

[Service]
Type=oneshot
ExecStart=$NEURON_ADVERTISER

[Install]
WantedBy=k3s.service
EOF
        install -m 0644 "$WORK/fabric-neuron-advertise.service" "$NEURON_UNIT"
        systemctl daemon-reload
        systemctl enable fabric-neuron-advertise.service
        "$NEURON_ADVERTISER" || die "could not advertise $NEURON_RESOURCE on node $LOCAL_NODE; inspect journalctl -u k3s"
        for attempt in $(seq 1 60); do
            ALLOCATABLE=$("${KUBECTL[@]}" get node "$LOCAL_NODE" -o jsonpath='{.status.allocatable.aws\.amazon\.com/neuron}')
            [ "$ALLOCATABLE" = "$NEURON_COUNT" ] && break
            [ "$attempt" -lt 60 ] || die "allocatable Neuron devices are ${ALLOCATABLE:-0}, found $NEURON_COUNT; inspect journalctl -u k3s"
            sleep 2
        done
        log "$ALLOCATABLE Neuron devices are allocatable. Model pods must request $NEURON_RESOURCE and run privileged with /dev/neuron*."
    fi
fi

if command -v helm >/dev/null 2>&1; then
    log 'Reusing existing Helm without replacing it'
else
    download "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" "$WORK/helm.tar.gz"
    printf '%s  %s\n' "$HELM_SHA256" "$WORK/helm.tar.gz" | sha256sum --check --status \
        || die 'Helm archive SHA-256 mismatch'
    tar -xzf "$WORK/helm.tar.gz" -C "$WORK" "linux-$ARCH/helm"
    [ ! -e /usr/local/bin/helm ] && [ ! -L /usr/local/bin/helm ] \
        || die 'an existing /usr/local/bin/helm needs review before installing Helm'
    install -m 0755 "$WORK/linux-$ARCH/helm" /usr/local/bin/helm
fi

log "k3s node $LOCAL_NODE is Ready. Local kubeconfig: $KUBECONFIG_PATH"
log "Inspect it: sudo k3s kubectl --kubeconfig $KUBECONFIG_PATH --context default get nodes"
log "Use Helm: sudo helm --kubeconfig $KUBECONFIG_PATH --kube-context default list -A"
log 'Next: run the Fabric enrollment Helm command from your Infrastructure dialog on this VM.'
