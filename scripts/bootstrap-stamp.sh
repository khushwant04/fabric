#!/usr/bin/env bash
# One command to turn an Ubuntu GPU VM, a single-host Cloud TPU VM, or an AWS Inferentia2 or
# Trainium instance, into a Fabric inference stamp.
#
#   curl -fsSL https://raw.githubusercontent.com/khushwant04/fabric/main/scripts/bootstrap-stamp.sh \
#     | sudo bash -s -- --name gpu-01 --control-plane https://api.example.com --token '<token>' \
#         --host gpu-01.stamps.example.com --email ops@example.com
#
# It installs k3s with NVIDIA support, checks that the GPU runtime works, sets up cert-manager
# and a Let's Encrypt issuer (when --host is given), stores the single-use enrollment token in a
# Secret, installs the stamp chart and images of one pinned release, and waits until the agent
# has enrolled. Progress is printed as numbered steps; the detailed output of each step goes to
# a log file. Re-running it is safe: it upgrades the stamp in place and keeps the agent's
# identity, so no new token is needed.
#
# On a Cloud TPU VM (detected from the GCP metadata server) it installs no NVIDIA software:
# it advertises the TPU chips to Kubernetes and sets the chart's TPU values instead, so models
# are served by the vLLM TPU image (docs/tpu.md).
#
# On an AWS inf2, trn1 or trn2 instance (detected from EC2 instance metadata) it installs no
# NVIDIA software either: it installs the Neuron device plugin and sets the chart's Neuron
# values, so models are served by vLLM on Neuron (docs/neuron.md).
#
# Nothing is pre-pulled: the customer picks a model in the console and watches the model
# runtime image and the weights download live in the deployment's Logs tab.
set -Eeuo pipefail
umask 077

# Run as `curl ... | sudo bash -s -- ...`, bash reads this script from the same stdin its
# commands inherit. Everything lives in main(), so bash has read the whole file before the first
# command runs: no command can consume the rest of the script or block reading it.
main() {

REPOSITORY=https://github.com/khushwant04/fabric
# Chart and images come from the same commit. CI publishes images for every commit on main.
DEFAULT_RELEASE=6e8199737e76245cef21def1906c68d589b164b9
DEFAULT_REGISTRY=public.ecr.aws/hexelstudio/fabric
CERT_MANAGER_VERSION=v1.17.2
NAMESPACE=fabric-stamp
SECRET=stamp-enrollment
ISSUER=fabric-letsencrypt
KUBECONFIG_PATH=/etc/rancher/k3s/k3s.yaml
WORK_ROOT=/opt/fabric
LOG_FILE=/var/log/fabric-stamp-bootstrap.log
TPU_METADATA_URL=http://metadata.google.internal/computeMetadata/v1/instance/attributes/accelerator-type
EC2_METADATA_URL=http://169.254.169.254/latest

NAME="" CONTROL_PLANE="" ISSUER_URL="" TOKEN="${FABRIC_ENROLLMENT_TOKEN:-}" HOST="" EMAIL=""
REGION="" RELEASE="$DEFAULT_RELEASE" REGISTRY="$DEFAULT_REGISTRY" GPU=auto DTYPE="" PREPULL=""
CONSOLE_URL="${FABRIC_CONSOLE_URL:-}" DRY_RUN=0 VERBOSE=1 TPU_TYPE="" TPU_DEVICES=""
NEURON_TYPE="" NEURON_DEVICES=""

usage() {
    cat <<'EOF'
Usage: sudo bash bootstrap-stamp.sh --name NAME --control-plane URL [options]

Required:
  --name NAME             stamp name: lowercase letters, digits and hyphens (also the node name)
  --control-plane URL     HTTPS origin of the Fabric control plane, e.g. https://api.example.com
  --token TOKEN           single-use enrollment token from the console (or FABRIC_ENROLLMENT_TOKEN);
                          not needed when re-running on a stamp that has already enrolled

Public HTTPS endpoint (needed for the edge gateway to route to this stamp):
  --host HOSTNAME         DNS name pointing at this VM's public IP, ports 80 and 443 open
  --email EMAIL           contact for the Let's Encrypt certificate (required with --host)

Optional:
  --region REGION         reported to the control plane for placement, e.g. asia-southeast1
  --console URL           console origin, so the summary links straight to this stamp's page
  --dtype bfloat16|float16  model dtype; default: bfloat16 on compute capability 8.0+, else float16
  --no-gpu                install without NVIDIA or TPU support (control and testing only)
  --tpu TYPE              Cloud TPU accelerator type, e.g. v6e-4; only needed when the GCP
                          metadata server cannot be read (it is detected automatically)
  --neuron TYPE           AWS Inferentia2/Trainium instance type, e.g. inf2.xlarge; only needed
                          when EC2 instance metadata cannot be read (it is detected automatically)
  --issuer URL            JWT issuer, when it differs from --control-plane
  --release SHA           Fabric commit whose chart and images to install (default: pinned)
  --registry REGISTRY     image registry (default: public.ecr.aws/hexelstudio/fabric)
  --dry-run               print the plan and the Helm command, change nothing
  --quiet                 show only the numbered steps; detailed output stays in the log

Steps: 1 Checking GPU, 2 Installing k3s, 3 GPU runtime, 4 TLS, 5 Enrolling, 6 Done.
Detailed output goes to /var/log/fabric-stamp-bootstrap.log.

Requirements: Ubuntu 22.04/24.04 on x86_64 with systemd; for GPUs, a working NVIDIA driver
(nvidia-smi); for Cloud TPU, a single-host TPU VM (v4, v5e, v5p, v6e) on a TPU Ubuntu image;
for AWS Neuron, an inf2, trn1 or trn2 instance on the Neuron Deep Learning AMI (Ubuntu).
Outbound HTTPS to the control plane, GitHub, the registry and Hugging Face.
EOF
}

# Reads one EC2 instance metadata path with IMDSv2 (a session token first, as instances that
# require IMDSv2 refuse plain reads). Prints nothing and fails off EC2.
ec2_metadata() {
    local token
    token=$(curl -fsS -m 2 -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' "$EC2_METADATA_URL/api/token" 2>/dev/null) || return 1
    [ -n "$token" ] || return 1
    curl -fsS -m 2 -H "X-aws-ec2-metadata-token: $token" "$EC2_METADATA_URL/meta-data/$1" 2>/dev/null
}

# --------------------------------------------------------------------------- output
TOTAL_STEPS=6
STEP=0
STEP_NAME=""
if [ -t 1 ]; then GREEN=$'\033[32m' RED=$'\033[31m' YELLOW=$'\033[33m' BOLD=$'\033[1m' DIM=$'\033[2m' RESET=$'\033[0m'
else GREEN="" RED="" YELLOW="" BOLD="" DIM="" RESET=""; fi

step() { STEP=$((STEP + 1)); STEP_NAME=$1; printf '\n%s[%d/%d] %s%s\n' "$BOLD" "$STEP" "$TOTAL_STEPS" "$1" "$RESET"; }
ok() { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail() { printf '  %s✗%s %s\n' "$RED" "$RESET" "$*" >&2; }
die() {
    fail "$*"
    if [ -n "$STEP_NAME" ]; then
        printf '\n%s%s failed.%s Details: %s\n' "$RED" "$STEP_NAME" "$RESET" "$LOG_FILE" >&2
        printf 'Fix the cause and run the same command again; it is safe to re-run.\n' >&2
    fi
    exit 1
}
# Detailed output of a step goes to the log, so the numbered steps stay readable.
# Runs a step's command with its output streamed live, indented under the step (and kept in the
# log), and a heartbeat every 20 seconds so a long wait never looks like a hang. --quiet keeps
# only the log.
quiet() {
    local started=$SECONDS heartbeat status=0
    ( while sleep 20; do printf '    %s… still working (%ss)%s\n' "$DIM" $((SECONDS - started)) "$RESET"; done ) &
    heartbeat=$!
    if [ "$VERBOSE" -eq 1 ]; then
        "$@" 2>&1 | tee -a "$LOG_FILE" | while IFS= read -r line; do printf '    %s│ %s%s\n' "$DIM" "${line:0:200}" "$RESET"; done || status=$?
    else
        "$@" >>"$LOG_FILE" 2>&1 || status=$?
    fi
    kill "$heartbeat" 2>/dev/null || true
    wait "$heartbeat" 2>/dev/null || true
    return "$status"
}
on_error() {
    local status=$? line=$1
    trap - ERR
    fail "${STEP_NAME:-setup} stopped (exit $status at line $line)"
    if [ -f "$LOG_FILE" ]; then
        printf '\nLast lines of %s:\n' "$LOG_FILE" >&2
        tail -n 15 "$LOG_FILE" | sed 's/^/    /' >&2
    fi
    printf '\nFix the cause and run the same command again; it is safe to re-run.\n' >&2
    exit "$status"
}
need_value() { [ "$#" -ge 2 ] && [ -n "$2" ] || die "$1 requires a value"; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --name) need_value "$@"; NAME=$2; shift 2 ;;
        --control-plane) need_value "$@"; CONTROL_PLANE=${2%/}; shift 2 ;;
        --issuer) need_value "$@"; ISSUER_URL=$2; shift 2 ;;
        --token) need_value "$@"; TOKEN=$2; shift 2 ;;
        --host) need_value "$@"; HOST=$2; shift 2 ;;
        --email) need_value "$@"; EMAIL=$2; shift 2 ;;
        --region) need_value "$@"; REGION=$2; shift 2 ;;
        --console) need_value "$@"; CONSOLE_URL=${2%/}; shift 2 ;;
        # Opt-in and deliberately not in --help: pre-pulling hides the first download the
        # customer is meant to watch. Kept so existing commands keep working; it turns the
        # chart's pre-pull DaemonSet on for these repositories (docs/model-host-prepull.md).
        --prepull) need_value "$@"; PREPULL=$2; shift 2 ;;
        --dtype) need_value "$@"; DTYPE=$2; shift 2 ;;
        --release) need_value "$@"; RELEASE=$2; shift 2 ;;
        --registry) need_value "$@"; REGISTRY=${2%/}; shift 2 ;;
        --no-gpu) GPU=off; shift ;;
        --tpu) need_value "$@"; TPU_TYPE=$2; GPU=tpu; shift 2 ;;
        --neuron) need_value "$@"; NEURON_TYPE=$2; GPU=neuron; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --quiet) VERBOSE=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done

# --------------------------------------------------------------------------- validation
[[ "$NAME" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] && [ "${#NAME}" -le 53 ] \
    || die '--name must be a lowercase DNS label of at most 53 characters'
[[ "$CONTROL_PLANE" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] \
    || die '--control-plane must be an HTTPS origin such as https://api.example.com'
ISSUER_URL=${ISSUER_URL:-$CONTROL_PLANE}
[[ "$ISSUER_URL" =~ ^https:// ]] || die '--issuer must be an HTTPS URL'
if [ -n "$HOST" ]; then
    [[ "$HOST" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)+$ ]] \
        || die '--host must be a DNS name without https:// or a path'
    [[ "$EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] \
        || die '--email is required with --host (Let'"'"'s Encrypt contact address)'
fi
[ -z "$CONSOLE_URL" ] || [[ "$CONSOLE_URL" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] \
    || die '--console must be an origin such as https://console.example.com'
[[ "$RELEASE" =~ ^[0-9a-f]{40}$ ]] || die '--release must be a full 40-character commit SHA'
[ -z "$REGION" ] || [[ "$REGION" =~ ^[a-z0-9-]{1,63}$ ]] || die '--region must be lowercase letters, digits and hyphens'
[ -z "$DTYPE" ] || [[ "$DTYPE" =~ ^(bfloat16|float16)$ ]] || die '--dtype must be bfloat16 or float16'
[ -z "$PREPULL" ] || [[ "$PREPULL" =~ ^[A-Za-z0-9._/-]+(,[A-Za-z0-9._/-]+)*$ ]] \
    || die '--prepull must be comma-separated repositories such as Qwen/Qwen3.5-4B'
[ -z "$TOKEN" ] || [[ "$TOKEN" != *[[:space:]]* ]] || die 'the enrollment token must not contain whitespace'
[ -z "$TPU_TYPE" ] || [[ "$TPU_TYPE" =~ ^[a-z0-9]+-[0-9]+$ ]] || die '--tpu must be an accelerator type such as v6e-4'
[ -z "$NEURON_TYPE" ] || [[ "$NEURON_TYPE" =~ ^[a-z0-9]+\.[a-z0-9]+$ ]] || die '--neuron must be an EC2 instance type such as inf2.xlarge'
[ -z "$TPU_TYPE" ] || [ -z "$NEURON_TYPE" ] || die '--tpu and --neuron are mutually exclusive'

printf '%sFabric stamp %s%s → %s\n' "$BOLD" "$NAME" "$RESET" "$CONTROL_PLANE"

# --------------------------------------------------------------------------- 1. preflight
step "Checking prerequisites"
CP_HOST=${CONTROL_PLANE#https://}; CP_HOST=${CP_HOST%%:*}
if [ -n "$HOST" ] && [ "$CP_HOST" = "$HOST" ]; then
    die "--control-plane and --host are the same name ($HOST). --control-plane is the Fabric API this stamp joins (for example https://api.example.com); --host is this stamp's own public name."
fi
# A Fabric control plane publishes its token-signing keys; anything else is the wrong URL.
if JWKS=$(curl --proto '=https' --tlsv1.2 -fsS -m 15 "$CONTROL_PLANE/.well-known/jwks.json" 2>&1) \
    && printf '%s' "$JWKS" | grep -q '"keys"'; then
    ok "control plane $CONTROL_PLANE is reachable"
else
    die "$CONTROL_PLANE is not a reachable Fabric control plane (no keys at /.well-known/jwks.json). Use the console's control-plane address, for example https://api.example.com. The stamp's own hostname belongs in --host."
fi
if [ -n "$TOKEN" ]; then
    [[ "$TOKEN" =~ ^fab_enroll_[A-Za-z0-9_-]{20,}$ ]] \
        || die 'the enrollment token should look like fab_enroll_...; copy it again from console -> Inference stamps -> Enroll stamp'
fi
if [ -n "$HOST" ]; then
    RESOLVED=$(getent ahostsv4 "$HOST" 2>/dev/null | awk 'NR == 1 {print $1}' || true)
    # GCP and most clouds expose the external address on their metadata service.
    PUBLIC_IP=$(curl -fsS -m 3 -H 'Metadata-Flavor: Google' \
        http://169.254.169.254/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null \
        || curl -fsS -m 3 -H 'Metadata: true' \
        'http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/publicIpAddress?api-version=2021-02-01&format=text' 2>/dev/null \
        || ec2_metadata public-ipv4 \
        || true)
    if [ -z "$RESOLVED" ]; then
        warn "$HOST does not resolve yet. Create a DNS A record for it${PUBLIC_IP:+ pointing at $PUBLIC_IP}; the TLS certificate is issued once it resolves."
    elif [ -n "$PUBLIC_IP" ] && [ "$RESOLVED" != "$PUBLIC_IP" ]; then
        warn "$HOST resolves to $RESOLVED, but this machine's public IP is $PUBLIC_IP. Update the DNS A record, or the certificate cannot be issued."
    else
        ok "$HOST resolves to ${RESOLVED}${PUBLIC_IP:+ (this machine)}"
    fi
    note "inbound TCP 80 and 443 must be open to this machine for the certificate and for inference"
fi

GPU_REPORT=""
if [ "$GPU" = auto ]; then
    if command -v nvidia-smi >/dev/null 2>&1; then GPU=on
    else
        # A Cloud TPU VM has no NVIDIA driver; its metadata names the TPU slice, e.g. v6e-4.
        TPU_TYPE=$(curl -fsS -m 3 -H 'Metadata-Flavor: Google' "$TPU_METADATA_URL" 2>/dev/null | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]' || true)
        if [[ "$TPU_TYPE" =~ ^[a-z0-9]+-[0-9]+$ ]]; then GPU=tpu; else TPU_TYPE=""; GPU=off; fi
        if [ "$GPU" = off ]; then
            # An AWS Inferentia2 or Trainium instance has no NVIDIA driver either; EC2 instance
            # metadata names its instance type, e.g. inf2.xlarge.
            NEURON_TYPE=$(ec2_metadata instance-type | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]' || true)
            if [[ "$NEURON_TYPE" =~ ^(inf2|trn1|trn1n|trn2|trn2u)\.[a-z0-9]+$ ]]; then GPU=neuron; else NEURON_TYPE=""; fi
        fi
    fi
fi
if [ "$GPU" = tpu ]; then
    # v4 and v5p accelerator types count TensorCores, two per chip; v5e and v6e count chips.
    case "${TPU_TYPE%-*}" in
        v4) TPU_PRODUCT="TPU v4" TPU_HBM_GIB=32 TPU_CORES_PER_CHIP=2 ;;
        v5p) TPU_PRODUCT="TPU v5p" TPU_HBM_GIB=95 TPU_CORES_PER_CHIP=2 ;;
        v5e|v5litepod) TPU_PRODUCT="TPU v5e" TPU_HBM_GIB=16 TPU_CORES_PER_CHIP=1 ;;
        v6e) TPU_PRODUCT="TPU v6e" TPU_HBM_GIB=32 TPU_CORES_PER_CHIP=1 ;;
        *) die "Cloud TPU $TPU_TYPE is not supported; use v6e, v5e (v5litepod), v5p or v4" ;;
    esac
    TPU_EXPECTED=$(( ${TPU_TYPE##*-} / TPU_CORES_PER_CHIP ))
    [ "$TPU_EXPECTED" -ge 1 ] || TPU_EXPECTED=1
    # v4 and v5e chips appear as /dev/accelN; v5p and v6e chips as VFIO groups /dev/vfio/N.
    ACCEL_NODES=$(ls -1d /dev/accel[0-9]* 2>/dev/null || true)
    VFIO_NODES=$(ls -1 /dev/vfio 2>/dev/null | grep -E '^[0-9]+$' || true)
    if [ -n "$ACCEL_NODES" ]; then
        TPU_CHIPS=$(printf '%s\n' "$ACCEL_NODES" | awk 'NF {n++} END {print n + 0}')
        TPU_DEVICES=$(printf '%s\n' "$ACCEL_NODES" | paste -sd, -)
    elif [ -n "$VFIO_NODES" ]; then
        TPU_CHIPS=$(printf '%s\n' "$VFIO_NODES" | awk 'NF {n++} END {print n + 0}')
        TPU_DEVICES=/dev/vfio
    else
        die "this is a Cloud TPU VM ($TPU_TYPE) but no TPU device nodes are visible (/dev/accel* or /dev/vfio/N). Use a TPU VM image (tpu-ubuntu2204-base or the v2-alpha-tpu* images), or pass --no-gpu"
    fi
    [ "$TPU_EXPECTED" -le "$TPU_CHIPS" ] \
        || die "$TPU_TYPE has $TPU_EXPECTED chips across several hosts and this host sees $TPU_CHIPS: multi-host TPU slices are not supported. Create a single-host type such as v6e-4 or v5litepod-4"
    [ -z "$DTYPE" ] || [ "$DTYPE" = bfloat16 ] || die "--dtype $DTYPE is not supported on Cloud TPU, which serves bfloat16"
    DTYPE=bfloat16
    ok "$TPU_CHIPS × $TPU_PRODUCT ($TPU_TYPE, $TPU_HBM_GIB GiB HBM per chip, $(( TPU_CHIPS * TPU_HBM_GIB )) GiB total)"
    ok "model dtype: ${DTYPE} (Cloud TPU serves bf16 checkpoints; FP8 models need an NVIDIA GPU)"
elif [ "$GPU" = neuron ]; then
    NEURON_TYPE=$(printf '%s' "$NEURON_TYPE" | tr '[:upper:]' '[:lower:]')
    # Per device: HBM, physical NeuronCores, and the NeuronCores vLLM sees at the runtime's
    # default logical-core setting (Trainium2 pairs its eight cores into four, LNC=2).
    case "${NEURON_TYPE%%.*}" in
        inf2) NEURON_PRODUCT="Inferentia2" NEURON_HBM_GIB=32 NEURON_CORES=2 NEURON_TP_CORES=2 ;;
        trn1|trn1n) NEURON_PRODUCT="Trainium" NEURON_HBM_GIB=32 NEURON_CORES=2 NEURON_TP_CORES=2 ;;
        trn2|trn2u) NEURON_PRODUCT="Trainium2" NEURON_HBM_GIB=96 NEURON_CORES=8 NEURON_TP_CORES=4 ;;
        inf1) die "Inferentia1 ($NEURON_TYPE) has no vLLM backend; use an inf2, trn1 or trn2 instance" ;;
        *) die "AWS instance type $NEURON_TYPE is not a supported Neuron instance; use inf2, trn1 or trn2" ;;
    esac
    case "$NEURON_TYPE" in
        inf2.xlarge|inf2.8xlarge|trn1.2xlarge|trn2.3xlarge) NEURON_EXPECTED=1 ;;
        inf2.24xlarge) NEURON_EXPECTED=6 ;;
        inf2.48xlarge) NEURON_EXPECTED=12 ;;
        trn1.32xlarge|trn1n.32xlarge|trn2.48xlarge|trn2u.48xlarge) NEURON_EXPECTED=16 ;;
        *) NEURON_EXPECTED=0 ;;
    esac
    # The Neuron driver (aws-neuronx-dkms, shipped on the Neuron Deep Learning AMI) creates
    # one /dev/neuronN per device.
    NEURON_NODES=$(ls -1d /dev/neuron[0-9]* 2>/dev/null | sort -V || true)
    NEURON_COUNT=$(printf '%s\n' "$NEURON_NODES" | awk 'NF {n++} END {print n + 0}')
    [ "$NEURON_COUNT" -gt 0 ] \
        || die "this is an AWS $NEURON_TYPE instance but no Neuron devices are visible (/dev/neuron*). Use the Deep Learning AMI Neuron (Ubuntu 22.04 or 24.04), which ships the aws-neuronx-dkms driver, or install the Neuron driver; or pass --no-gpu"
    NEURON_DEVICES=$(printf '%s\n' "$NEURON_NODES" | paste -sd, -)
    if [ "$NEURON_EXPECTED" -gt 0 ] && [ "$NEURON_COUNT" -ne "$NEURON_EXPECTED" ]; then
        warn "$NEURON_TYPE has $NEURON_EXPECTED Neuron devices, but this host sees $NEURON_COUNT; check neuron-ls"
    fi
    [ -z "$DTYPE" ] || [ "$DTYPE" = bfloat16 ] || die "--dtype $DTYPE is not supported on AWS Neuron stamps, which serve bfloat16"
    DTYPE=bfloat16
    if [ "$NEURON_COUNT" -eq 1 ]; then
        ok "1 × $NEURON_PRODUCT ($NEURON_TYPE, $NEURON_HBM_GIB GiB HBM, $NEURON_CORES NeuronCores)"
    else
        ok "$NEURON_COUNT × $NEURON_PRODUCT ($NEURON_TYPE, $NEURON_HBM_GIB GiB HBM per device, $(( NEURON_COUNT * NEURON_HBM_GIB )) GiB total, $(( NEURON_COUNT * NEURON_CORES )) NeuronCores)"
    fi
    ok "model dtype: ${DTYPE} (Neuron stamps serve bf16 checkpoints; FP8 models need an NVIDIA GPU)"
elif [ "$GPU" = on ]; then
    GPU_REPORT=$(timeout 20s nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv,noheader,nounits 2>/dev/null) \
        || die 'nvidia-smi failed; repair the NVIDIA driver first, or pass --no-gpu'
    [ -n "$GPU_REPORT" ] || die 'nvidia-smi reports no GPUs'
    if [ -z "$DTYPE" ]; then
        # T4 and V100 (compute 7.x) have no BF16; Ampere and newer do.
        MIN_MAJOR=$(printf '%s\n' "$GPU_REPORT" | awk -F', *' '{split($3, v, "."); print v[1]}' | sort -n | head -1)
        if [ "${MIN_MAJOR:-0}" -ge 8 ]; then DTYPE=bfloat16; else DTYPE=float16; fi
    fi
    # "1 × NVIDIA RTX PRO 6000 Blackwell Server Edition (96 GiB, compute 12.0)" per model.
    printf '%s\n' "$GPU_REPORT" | awk -F', *' '{key = $1 " (" int($2 / 1024 + 0.5) " GiB, compute " $3 ")"; n[key]++}
        END {for (k in n) print n[k] " × " k}' | while IFS= read -r line; do ok "$line"; done
    ok "model dtype: ${DTYPE}"
else
    warn "no NVIDIA GPU support (--no-gpu or no nvidia-smi); the stamp will enroll without GPUs"
fi
DTYPE=${DTYPE:-float16}
TAG="git-$RELEASE"

# --------------------------------------------------------------------------- the stamp's values
HELM_ARGS=(
    --namespace "$NAMESPACE"
    --set-string "controlPlane.url=$CONTROL_PLANE"
    --set-string "controlPlane.jwtIssuer=$ISSUER_URL"
    --set-string "enrollment.existingSecret=$SECRET"
    --set-string "enrollment.existingSecretKey=enrollment-token"
    --set-string "stamp.name=$NAME"
    --set-string "stamp.orchestrator=k3s"
    --set-string "image.agent.repository=$REGISTRY/agent"
    --set-string "image.agent.tag=$TAG"
    --set-string "image.dataPlane.repository=$REGISTRY/data-plane"
    --set-string "image.dataPlane.tag=$TAG"
    --set operator.enabled=true
    --set-string "operator.managedModelHost.image=$REGISTRY/model-host:$TAG"
    --set-string "operator.managedModelHost.dtype=$DTYPE"
    # GCP and most VMs have no AKS-style ephemeral disk at /mnt.
    --set-string "operator.managedModelHost.cache.hostPath=/var/lib/fabric/model-cache"
    --set-string "persistence.storageClass=local-path"
    --set-string "usageSpool.storageClass=local-path"
)
[ -z "$REGION" ] || HELM_ARGS+=(--set-string "stamp.region=$REGION")
if [ "$GPU" = on ]; then
    # k3s runs GPU containers only under the nvidia RuntimeClass; without it vLLM reports
    # "Failed to infer device type". The bootstrap labels GPU nodes accelerator=nvidia.
    HELM_ARGS+=(--set-string gpu.runtimeClassName=nvidia --set-string gpu.nodeSelector.accelerator=nvidia
        --set gpuMetrics.dcgmExporter.enabled=true)
elif [ "$GPU" = tpu ]; then
    # No RuntimeClass, device plugin or DCGM exporter: the chart runs the vLLM TPU image,
    # privileged, with these device nodes. The bootstrap labels the TPU node accelerator=tpu.
    HELM_ARGS+=(--set-string accelerator.type=tpu --set-string gpu.nodeSelector.accelerator=tpu
        --set "accelerator.tpu.devices={$TPU_DEVICES}")
elif [ "$GPU" = neuron ]; then
    # No RuntimeClass or DCGM exporter: the chart runs vLLM on Neuron and requests
    # aws.amazon.com/neuron devices from the Neuron device plugin, which bootstrap-k3s.sh
    # installs. The bootstrap labels the Neuron node accelerator=neuron.
    HELM_ARGS+=(--set-string accelerator.type=neuron --set-string gpu.nodeSelector.accelerator=neuron
        --set "accelerator.neuron.coresPerDevice=$NEURON_TP_CORES")
fi
if [ -n "$PREPULL" ]; then
    HELM_ARGS+=(--set operator.managedModelHost.prepull.enabled=true
        --set "operator.managedModelHost.prepull.models={$PREPULL}")
else
    # Explicit, because a pinned release's chart may predate pre-pull being off by default.
    HELM_ARGS+=(--set operator.managedModelHost.prepull.enabled=false)
fi
if [ -n "$HOST" ]; then
    HELM_ARGS+=(
        --set exposure.enabled=true
        --set-string "exposure.host=$HOST"
        --set-string exposure.className=traefik
        --set-string "exposure.tls.issuer.name=$ISSUER"
        --set-string exposure.tls.issuer.kind=ClusterIssuer
    )
fi

if [ "$DRY_RUN" -eq 1 ]; then
    printf '\nDry run: release %s, images %s/*:%s, public endpoint %s\n' \
        "${RELEASE:0:12}" "$REGISTRY" "$TAG" "$( [ -n "$HOST" ] && printf "https://%s" "$HOST" || printf none )"
    printf 'helm upgrade --install %q <chart:%s/deploy/helm/fabric-stamp>' "$NAME" "${RELEASE:0:12}"
    printf ' %q' "${HELM_ARGS[@]}"
    printf '\n'
    exit 0
fi

[ "$(id -u)" -eq 0 ] || die 'run with sudo'
for tool in curl tar; do command -v "$tool" >/dev/null 2>&1 || die "$tool is required"; done
mkdir -p "$(dirname "$LOG_FILE")"
: >>"$LOG_FILE" && chmod 600 "$LOG_FILE"
printf '\n==== %s bootstrap %s release %s\n' "$(date -u +%FT%TZ)" "$NAME" "${RELEASE:0:12}" >>"$LOG_FILE"
trap 'on_error $LINENO' ERR

# --------------------------------------------------------------------------- 2. k3s
step "Installing k3s"
RELEASE_DIR="$WORK_ROOT/releases/$RELEASE"
if [ ! -f "$RELEASE_DIR/deploy/helm/fabric-stamp/Chart.yaml" ]; then
    mkdir -p "$WORK_ROOT/releases"
    ARCHIVE=$(mktemp "$WORK_ROOT/release.XXXXXX.tar.gz")
    quiet curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 3 \
        "$REPOSITORY/archive/$RELEASE.tar.gz" -o "$ARCHIVE"
    STAGE=$(mktemp -d "$WORK_ROOT/stage.XXXXXX")
    tar -xzf "$ARCHIVE" -C "$STAGE" --strip-components=1
    rm -rf "$RELEASE_DIR" && mv "$STAGE" "$RELEASE_DIR" && rm -f "$ARCHIVE"
    ok "downloaded Fabric release ${RELEASE:0:12}"
else
    ok "Fabric release ${RELEASE:0:12} already downloaded"
fi
if [ "$GPU" = tpu ] && ! grep -q -- '--tpu' "$RELEASE_DIR/scripts/bootstrap-k3s.sh"; then
    die "release ${RELEASE:0:12} predates Cloud TPU support. Pass --release with a newer commit of $REPOSITORY (docs/tpu.md)"
fi
if [ "$GPU" = neuron ] && ! grep -q -- '--neuron' "$RELEASE_DIR/scripts/bootstrap-k3s.sh"; then
    die "release ${RELEASE:0:12} predates AWS Neuron support. Pass --release with a newer commit of $REPOSITORY (docs/neuron.md)"
fi

K3S_ARGS=()
# Name the node only on a fresh install: an existing server keeps its node name.
systemctl cat k3s.service >/dev/null 2>&1 || K3S_ARGS+=(--node-name "$NAME")
[ "$GPU" = on ] && K3S_ARGS+=(--nvidia)
[ "$GPU" = tpu ] && K3S_ARGS+=(--tpu "$TPU_TYPE")
[ "$GPU" = neuron ] && K3S_ARGS+=(--neuron "$NEURON_TYPE")
if ! quiet bash "$RELEASE_DIR/scripts/bootstrap-k3s.sh" "${K3S_ARGS[@]}"; then
    # A brand-new server needs a few seconds after it reports Ready before its node and the
    # nvidia RuntimeClass exist; older pinned releases check only once. Wait, then retry.
    systemctl is-active --quiet k3s || die "k3s did not start; see $LOG_FILE"
    note "k3s is still settling; waiting before one retry"
    for _ in $(seq 1 24); do
        if [ "$GPU" != on ] || k3s kubectl --kubeconfig "$KUBECONFIG_PATH" get runtimeclass nvidia >/dev/null 2>&1; then break; fi
        sleep 5
    done
    # The server now exists and keeps its node name, so the retry passes no --node-name.
    K3S_ARGS=()
    [ "$GPU" = on ] && K3S_ARGS+=(--nvidia)
    [ "$GPU" = tpu ] && K3S_ARGS+=(--tpu "$TPU_TYPE")
    [ "$GPU" = neuron ] && K3S_ARGS+=(--neuron "$NEURON_TYPE")
    quiet bash "$RELEASE_DIR/scripts/bootstrap-k3s.sh" "${K3S_ARGS[@]}"
fi
KUBECTL=(k3s kubectl --kubeconfig "$KUBECONFIG_PATH")
HELM=(helm --kubeconfig "$KUBECONFIG_PATH" --kube-context default)
ok "k3s $("${KUBECTL[@]}" version -o json 2>/dev/null | sed -n 's/.*"gitVersion": *"\([^"]*\)".*/\1/p' | tail -1) is running"

# --------------------------------------------------------------------------- 3. GPU runtime
if [ "$GPU" = tpu ]; then step "TPU runtime"; elif [ "$GPU" = neuron ]; then step "Neuron runtime"; else step "GPU runtime"; fi
GPUS=0
GPU_RUNTIME_OK=0
if [ "$GPU" = tpu ]; then
    # bootstrap-k3s.sh advertised the chips; there is no NVIDIA runtime or device plugin.
    for _ in $(seq 1 12); do
        GPUS=$("${KUBECTL[@]}" get nodes -o jsonpath='{range .items[*]}{.status.allocatable.google\.com/tpu}{"\n"}{end}' | awk '{s += $1} END {print s + 0}')
        [ "$GPUS" -gt 0 ] && break
        sleep 5
    done
    if [ "$GPUS" -gt 0 ]; then
        GPU_RUNTIME_OK=1
        ok "$GPUS google.com/tpu allocatable to Kubernetes (TPU chips, re-advertised whenever k3s starts)"
        note "models run the vLLM TPU image, privileged, with ${TPU_DEVICES//,/ }"
    else
        fail "no allocatable google.com/tpu: the TPU chips are not advertised to Kubernetes"
        note "Check: sudo systemctl status fabric-tpu-advertise; ls /dev/accel* /dev/vfio"
        note "then re-run this command. Continuing so the stamp enrolls;"
        note "the console's stamp page shows the same warning until this is fixed."
    fi
elif [ "$GPU" = neuron ]; then
    # bootstrap-k3s.sh installed the Neuron device plugin, or advertised the devices itself
    # when the plugin could not run.
    for _ in $(seq 1 36); do
        GPUS=$("${KUBECTL[@]}" get nodes -o jsonpath='{range .items[*]}{.status.allocatable.aws\.amazon\.com/neuron}{"\n"}{end}' | awk '{s += $1} END {print s + 0}')
        [ "$GPUS" -gt 0 ] && break
        sleep 5
    done
    PLUGIN_READY=$("${KUBECTL[@]}" -n kube-system get daemonset neuron-device-plugin-daemonset -o jsonpath='{.status.numberReady}' 2>/dev/null || true)
    if [ "$GPUS" -gt 0 ] && [ "${PLUGIN_READY:-0}" -gt 0 ]; then
        GPU_RUNTIME_OK=1
        ok "$GPUS aws.amazon.com/neuron allocatable to Kubernetes (Neuron device plugin)"
        note "models run vLLM on Neuron; the device plugin hands each model host its devices"
    elif [ "$GPUS" -gt 0 ]; then
        GPU_RUNTIME_OK=1
        # Without the plugin nothing injects the devices, so the model host opens them itself.
        HELM_ARGS+=(--set accelerator.neuron.privileged=true --set "accelerator.neuron.devices={$NEURON_DEVICES}")
        warn "$GPUS aws.amazon.com/neuron advertised without the Neuron device plugin (fabric-neuron-advertise)"
        note "models run vLLM on Neuron, privileged, with ${NEURON_DEVICES//,/ }"
        note "Check: sudo k3s kubectl -n kube-system get pods | grep neuron-device-plugin"
    else
        fail "no allocatable aws.amazon.com/neuron: the Neuron devices are not advertised to Kubernetes"
        note "Check: ls /dev/neuron*; neuron-ls; sudo k3s kubectl -n kube-system get pods | grep neuron"
        note "then re-run this command. Continuing so the stamp enrolls;"
        note "the console's stamp page shows the same warning until this is fixed."
    fi
elif [ "$GPU" = on ]; then
    if "${KUBECTL[@]}" get runtimeclass nvidia >/dev/null 2>&1; then
        ok "nvidia RuntimeClass present"
    else
        fail "nvidia RuntimeClass missing"
    fi
    # The device plugin needs a few seconds after k3s starts to advertise the GPUs.
    for _ in $(seq 1 36); do
        GPUS=$("${KUBECTL[@]}" get nodes -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' | awk '{s += $1} END {print s + 0}')
        [ "$GPUS" -gt 0 ] && break
        sleep 5
    done
    if [ "$GPUS" -gt 0 ]; then
        GPU_RUNTIME_OK=1
        ok "$GPUS nvidia.com/gpu allocatable to Kubernetes"
    else
        fail "no allocatable nvidia.com/gpu: the NVIDIA device plugin cannot see the GPU"
        note "Models would fail with \"Failed to infer device type\". Check:"
        note "  sudo k3s kubectl -n kube-system get pods | grep nvidia-device-plugin"
        note "  nvidia-smi; then re-run this command. Continuing so the stamp enrolls;"
        note "  the console's stamp page shows the same warning until this is fixed."
    fi
else
    warn "skipped: no GPU support requested"
fi

# --------------------------------------------------------------------------- 4. TLS
step "TLS"
if [ -n "$HOST" ]; then
    if ! "${KUBECTL[@]}" get crd clusterissuers.cert-manager.io >/dev/null 2>&1; then
        quiet "${HELM[@]}" upgrade --install cert-manager cert-manager \
            --repo https://charts.jetstack.io --version "$CERT_MANAGER_VERSION" \
            --namespace cert-manager --create-namespace --set crds.enabled=true --wait --timeout 10m
        ok "cert-manager $CERT_MANAGER_VERSION installed"
    else
        ok "cert-manager already installed"
    fi
    quiet "${KUBECTL[@]}" apply -f - <<EOF
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: $ISSUER
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: $EMAIL
    privateKeySecretRef:
      name: $ISSUER-account
    solvers:
      - http01:
          ingress:
            ingressClassName: traefik
EOF
    ok "Let's Encrypt issuer ready for https://$HOST"
else
    warn "skipped: no --host given. The stamp enrolls, but without a public HTTPS name the edge gateway cannot route requests to it. Re-run with --host NAME --email ADDRESS to add one."
fi

# --------------------------------------------------------------------------- 5. enrollment
step "Enrolling"
"${KUBECTL[@]}" get namespace "$NAMESPACE" >/dev/null 2>&1 || quiet "${KUBECTL[@]}" create namespace "$NAMESPACE"
INSTALLED=0
"${HELM[@]}" status "$NAME" --namespace "$NAMESPACE" >/dev/null 2>&1 && INSTALLED=1
if [ -n "$TOKEN" ]; then
    # Piped, never on a command line, so the token stays out of process listings and logs.
    printf '%s' "$TOKEN" | "${KUBECTL[@]}" -n "$NAMESPACE" create secret generic "$SECRET" \
        --from-file=enrollment-token=/dev/stdin --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f - >/dev/null
    TOKEN=""
    ok "enrollment token stored in Secret $NAMESPACE/$SECRET"
elif ! "${KUBECTL[@]}" -n "$NAMESPACE" get secret "$SECRET" >/dev/null 2>&1 && [ "$INSTALLED" -eq 0 ]; then
    die '--token is required for a first install (console -> Inference stamps -> Enroll stamp)'
fi

# An install interrupted with Ctrl-C leaves the release pending, which blocks every later
# upgrade. Roll back to the last good revision, or remove a first install that never finished.
RELEASE_STATUS=$("${HELM[@]}" status "$NAME" --namespace "$NAMESPACE" -o json 2>/dev/null \
    | sed -n 's/.*"status":"\([a-z-]*\)".*/\1/p' | head -1 || true)
case "$RELEASE_STATUS" in
    pending-*|failed)
        if "${HELM[@]}" history "$NAME" --namespace "$NAMESPACE" 2>/dev/null | grep -qw deployed; then
            quiet "${HELM[@]}" rollback "$NAME" --namespace "$NAMESPACE" --wait=false
            ok "recovered an interrupted install (rolled back to the last working revision)"
        else
            quiet "${HELM[@]}" uninstall "$NAME" --namespace "$NAMESPACE" --wait
            INSTALLED=0
            ok "removed an interrupted first install"
        fi ;;
esac

# No --wait: the agent's pod is only ready after it enrolls, so a wrong token or control plane
# would otherwise hang here for the whole timeout. Enrollment is watched below instead.
quiet "${HELM[@]}" upgrade --install "$NAME" "$RELEASE_DIR/deploy/helm/fabric-stamp" \
    "${HELM_ARGS[@]}" --timeout 15m
ok "stamp chart applied; waiting for the agent to enroll (this pulls the agent and gateway images)"

STAMP_ID=""
STARTED=$(date +%s)
LAST_NOTE=0
while :; do
    # By pod name: "logs statefulset/..." resolves pods by label, and the operator's pod shares
    # those labels, so kubectl can pick it and fail with "container agent is not valid".
    AGENT_LOG=$( { "${KUBECTL[@]}" -n "$NAMESPACE" logs "pod/$NAME-fabric-stamp-0" -c agent --previous --tail=200 2>/dev/null
                   "${KUBECTL[@]}" -n "$NAMESPACE" logs "pod/$NAME-fabric-stamp-0" -c agent 2>/dev/null; } || true)
    if printf '%s' "$AGENT_LOG" | grep -qE '"msg":"(enrolled|using persisted stamp identity)"'; then
        STAMP_ID=$(printf '%s' "$AGENT_LOG" | grep -oE '"stamp_id":"[0-9a-f-]{36}"' | tail -1 | cut -d'"' -f4 || true)
        break
    fi
    ENROLL_ERROR=$(printf '%s' "$AGENT_LOG" | grep '"msg":"enrollment failed"' | tail -1 \
        | sed -n 's/.*"error":"\(.*\)".*/\1/p' | sed 's/fab_[A-Za-z0-9_-]*/fab_…/g' | cut -c1-300 || true)
    if [ -n "$ENROLL_ERROR" ]; then
        fail "enrollment failed: $ENROLL_ERROR"
        case "$ENROLL_ERROR" in
            *401*|*403*|*nvalid*|*xpired*|*used*|*revoked*)
                note "The token was rejected: it is single use and expires. Generate a new one in the console" ;
                note "(Inference stamps -> Enroll stamp) and re-run with --token." ;;
            *404*|*"no such host"*|*refused*|*timeout*|*certificate*)
                note "The control plane at $CONTROL_PLANE did not answer as expected. Check --control-plane" ;
                note "is the console's control-plane address and that this machine can reach it over HTTPS." ;;
        esac
        die "fix the cause and run the same command again"
    fi
    POD_REASON=$("${KUBECTL[@]}" -n "$NAMESPACE" get pod "$NAME-fabric-stamp-0" \
        -o jsonpath='{range .status.containerStatuses[*]}{.state.waiting.reason}{" "}{end}' 2>/dev/null | tr -s ' ' || true)
    case "$POD_REASON" in
        *ImagePullBackOff*|*ErrImagePull*|*InvalidImageName*)
            die "the agent image could not be pulled ($POD_REASON). Check outbound HTTPS to the registry; release ${RELEASE:0:12} must have published images." ;;
    esac
    ELAPSED=$(( $(date +%s) - STARTED ))
    [ "$ELAPSED" -lt 900 ] || die "the agent did not enroll within 15 minutes; check: sudo k3s kubectl -n $NAMESPACE logs $NAME-fabric-stamp-0 -c agent"
    if [ $((ELAPSED - LAST_NOTE)) -ge 15 ]; then
        LAST_MSG=$(printf '%s' "$AGENT_LOG" | grep -oE '"msg":"[^"]*"' | tail -1 | cut -d'"' -f4 | cut -c1-120 || true)
        PHASE=$("${KUBECTL[@]}" -n "$NAMESPACE" get pod "$NAME-fabric-stamp-0" -o jsonpath='{.status.phase}' 2>/dev/null || true)
        note "waiting for enrollment (${ELAPSED}s): pod ${PHASE:-not created yet}${POD_REASON:+ ($POD_REASON)}${LAST_MSG:+; agent: $LAST_MSG}"
        LAST_NOTE=$ELAPSED
    fi
    sleep 5
done
if [ "$INSTALLED" -eq 1 ]; then ok "agent reused its enrolled identity (stamp $STAMP_ID)"
else ok "enrolled as stamp $STAMP_ID"; fi

if [ -n "$HOST" ]; then
    # Let's Encrypt validates over HTTP on port 80; report what it is doing while it does.
    CERT_READY=0
    for attempt in $(seq 1 30); do
        CERT_STATE=$("${KUBECTL[@]}" -n "$NAMESPACE" get certificate \
            -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{" "}{.status.conditions[?(@.type=="Ready")].message}{"\n"}{end}' 2>/dev/null | head -1 || true)
        case "$CERT_STATE" in True*) CERT_READY=1; break ;; esac
        if [ $((attempt % 2)) -eq 1 ]; then
            CHALLENGE=$("${KUBECTL[@]}" -n "$NAMESPACE" get challenges \
                -o jsonpath='{range .items[*]}{.status.state}{" "}{.status.reason}{"\n"}{end}' 2>/dev/null | head -1 | cut -c1-160 || true)
            note "certificate for $HOST: ${CHALLENGE:-${CERT_STATE:-waiting for cert-manager}} ($((attempt * 10))s)"
        fi
        sleep 10
    done
    if [ "$CERT_READY" -eq 1 ]; then
        ok "TLS certificate issued for $HOST"
    else
        warn "certificate not ready after 5 minutes. Let's Encrypt must reach http://$HOST on port 80: check the DNS A record and that inbound TCP 80 and 443 are open. It keeps retrying on its own."
    fi
fi

# --------------------------------------------------------------------------- 6. summary
step "Done"
trap - ERR
if [ "$GPU" = tpu ]; then
    if [ "$GPU_RUNTIME_OK" -eq 1 ]; then ok "stamp $NAME is enrolled and ready for deployments ($GPUS × $TPU_PRODUCT)"
    else fail "stamp $NAME is enrolled, but no TPU chip is allocatable yet (see step 3)"; fi
elif [ "$GPU" = neuron ]; then
    if [ "$GPU_RUNTIME_OK" -eq 1 ]; then ok "stamp $NAME is enrolled and ready for deployments ($GPUS × $NEURON_PRODUCT)"
    else fail "stamp $NAME is enrolled, but no Neuron device is allocatable yet (see step 3)"; fi
elif [ "$GPU" != on ]; then ok "stamp $NAME is enrolled without GPUs"
elif [ "$GPU_RUNTIME_OK" -eq 1 ]; then ok "stamp $NAME is enrolled and ready for deployments ($GPUS GPU(s))"
else fail "stamp $NAME is enrolled, but no GPU is allocatable yet (see step 3)"; fi
if [ -n "$CONSOLE_URL" ]; then
    STAMP_PAGE="$CONSOLE_URL/stamps/$STAMP_ID"
else
    STAMP_PAGE="console → Inference stamps → $NAME (/stamps/$STAMP_ID)"
fi
if [ "$GPU" = tpu ]; then
    cat <<EOF

  Stamp page:  $STAMP_PAGE
               Its Activity log shows: Enrolled → TPU measured → TPU runtime → Ready for deployments.
  Endpoint:    ${HOST:+https://$HOST}${HOST:-none (add --host for edge routing)}
  Next:        Deployments → Create, pick a model marked for Cloud TPU (bf16; FP8 models need a GPU),
               and choose infrastructure "$NAME". The first deployment downloads the vLLM TPU image
               once (several GB) and the weights, then compiles the model with XLA for several
               minutes before it answers; later starts reuse the compilation cache.
  Status:      sudo k3s kubectl -n $NAMESPACE get pods
  Details:     $LOG_FILE
  Re-run this command at any time to upgrade or repair the stamp; no new token is needed.
EOF
    return 0
fi
if [ "$GPU" = neuron ]; then
    cat <<EOF

  Stamp page:  $STAMP_PAGE
               Its Activity log shows: Enrolled → Neuron measured → Neuron runtime → Ready for deployments.
  Endpoint:    ${HOST:+https://$HOST}${HOST:-none (add --host for edge routing)}
  Next:        Deployments → Create, pick a model marked for AWS Neuron (bf16; FP8 models need a GPU),
               and choose infrastructure "$NAME". The first deployment downloads the vLLM Neuron
               image once (over 10 GB) and the weights, then compiles the model with the Neuron
               compiler, which can take tens of minutes; later starts reuse the compiled model
               kept on this instance.
  Status:      sudo k3s kubectl -n $NAMESPACE get pods
  Details:     $LOG_FILE
  Re-run this command at any time to upgrade or repair the stamp; no new token is needed.
EOF
    return 0
fi
cat <<EOF

  Stamp page:  $STAMP_PAGE
               Its Activity log shows: Enrolled → GPUs found → GPU runtime → Ready for deployments.
  Endpoint:    ${HOST:+https://$HOST}${HOST:-none (add --host for edge routing)}
  Next:        Deployments → Create, and choose infrastructure "$NAME". The console opens the
               deployment's Logs tab: the first deployment on this machine downloads the model
               runtime image once (several GB), then the model's weights.
  Status:      sudo k3s kubectl -n $NAMESPACE get pods
  Details:     $LOG_FILE
  Re-run this command at any time to upgrade or repair the stamp; no new token is needed.
EOF
}

main "$@"
