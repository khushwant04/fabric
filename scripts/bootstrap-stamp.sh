#!/usr/bin/env bash
# One command to turn an Ubuntu GPU VM into a Fabric inference stamp.
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
# Nothing is pre-pulled: the customer picks a model in the console and watches the model
# runtime image and the weights download live in the deployment's Logs tab.
set -Eeuo pipefail
umask 077

REPOSITORY=https://github.com/khushwant04/fabric
# Chart and images come from the same commit. CI publishes images for every commit on main.
DEFAULT_RELEASE=97ef926631f59de6dfd2f3a77a59ee29b8329dd6
DEFAULT_REGISTRY=public.ecr.aws/hexelstudio/fabric
CERT_MANAGER_VERSION=v1.17.2
NAMESPACE=fabric-stamp
SECRET=stamp-enrollment
ISSUER=fabric-letsencrypt
KUBECONFIG_PATH=/etc/rancher/k3s/k3s.yaml
WORK_ROOT=/opt/fabric
LOG_FILE=/var/log/fabric-stamp-bootstrap.log

NAME="" CONTROL_PLANE="" ISSUER_URL="" TOKEN="${FABRIC_ENROLLMENT_TOKEN:-}" HOST="" EMAIL=""
REGION="" RELEASE="$DEFAULT_RELEASE" REGISTRY="$DEFAULT_REGISTRY" GPU=auto DTYPE="" PREPULL=""
CONSOLE_URL="${FABRIC_CONSOLE_URL:-}" DRY_RUN=0

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
  --no-gpu                install without NVIDIA support (control and testing only)
  --issuer URL            JWT issuer, when it differs from --control-plane
  --release SHA           Fabric commit whose chart and images to install (default: pinned)
  --registry REGISTRY     image registry (default: public.ecr.aws/hexelstudio/fabric)
  --dry-run               print the plan and the Helm command, change nothing

Steps: 1 Checking GPU, 2 Installing k3s, 3 GPU runtime, 4 TLS, 5 Enrolling, 6 Done.
Detailed output goes to /var/log/fabric-stamp-bootstrap.log.

Requirements: Ubuntu 22.04/24.04 on x86_64 with systemd; for GPUs, a working NVIDIA driver
(nvidia-smi). Outbound HTTPS to the control plane, GitHub, the registry and Hugging Face.
EOF
}

# --------------------------------------------------------------------------- output
TOTAL_STEPS=6
STEP=0
STEP_NAME=""
if [ -t 1 ]; then GREEN=$'\033[32m' RED=$'\033[31m' YELLOW=$'\033[33m' BOLD=$'\033[1m' RESET=$'\033[0m'
else GREEN="" RED="" YELLOW="" BOLD="" RESET=""; fi

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
quiet() { "$@" >>"$LOG_FILE" 2>&1; }
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
        --dry-run) DRY_RUN=1; shift ;;
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

printf '%sFabric stamp %s%s → %s\n' "$BOLD" "$NAME" "$RESET" "$CONTROL_PLANE"

# --------------------------------------------------------------------------- 1. GPU
step "Checking GPU"
GPU_REPORT=""
if [ "$GPU" = auto ]; then
    if command -v nvidia-smi >/dev/null 2>&1; then GPU=on; else GPU=off; fi
fi
if [ "$GPU" = on ]; then
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

K3S_ARGS=()
# Name the node only on a fresh install: an existing server keeps its node name.
systemctl cat k3s.service >/dev/null 2>&1 || K3S_ARGS+=(--node-name "$NAME")
[ "$GPU" = on ] && K3S_ARGS+=(--nvidia)
quiet bash "$RELEASE_DIR/scripts/bootstrap-k3s.sh" "${K3S_ARGS[@]}"
KUBECTL=(k3s kubectl --kubeconfig "$KUBECONFIG_PATH")
HELM=(helm --kubeconfig "$KUBECONFIG_PATH" --kube-context default)
ok "k3s $("${KUBECTL[@]}" version -o json 2>/dev/null | sed -n 's/.*"gitVersion": *"\([^"]*\)".*/\1/p' | tail -1) is running"

# --------------------------------------------------------------------------- 3. GPU runtime
step "GPU runtime"
GPUS=0
GPU_RUNTIME_OK=0
if [ "$GPU" = on ]; then
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
    RESOLVED=$(getent ahostsv4 "$HOST" 2>/dev/null | awk 'NR == 1 {print $1}' || true)
    if [ -n "$RESOLVED" ]; then ok "$HOST resolves to $RESOLVED"
    else warn "$HOST does not resolve yet; the certificate is issued once DNS points here"; fi
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
    warn "skipped: no --host, so the edge gateway cannot route to this stamp"
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

quiet "${HELM[@]}" upgrade --install "$NAME" "$RELEASE_DIR/deploy/helm/fabric-stamp" \
    "${HELM_ARGS[@]}" --wait --timeout 15m
ok "stamp chart installed (agent and gateway running)"

STAMP_ID=""
for _ in $(seq 1 60); do
    AGENT_LOG=$("${KUBECTL[@]}" -n "$NAMESPACE" logs "statefulset/$NAME-fabric-stamp" -c agent --tail=400 2>/dev/null || true)
    if printf '%s' "$AGENT_LOG" | grep -qE '"msg":"(enrolled|using persisted stamp identity)"'; then
        STAMP_ID=$(printf '%s' "$AGENT_LOG" | grep -oE '"stamp_id":"[0-9a-f-]{36}"' | tail -1 | cut -d'"' -f4)
        break
    fi
    sleep 5
done
[ -n "$STAMP_ID" ] || die "the agent did not enroll; check: sudo k3s kubectl -n $NAMESPACE logs statefulset/$NAME-fabric-stamp -c agent"
if [ "$INSTALLED" -eq 1 ]; then ok "agent reused its enrolled identity (stamp $STAMP_ID)"
else ok "enrolled as stamp $STAMP_ID"; fi

if [ -n "$HOST" ]; then
    if "${KUBECTL[@]}" -n "$NAMESPACE" wait --for=condition=Ready certificate --all --timeout=300s >/dev/null 2>&1; then
        ok "TLS certificate issued for $HOST"
    else
        warn "certificate not ready yet; check DNS for $HOST and that ports 80/443 are open"
    fi
fi

# --------------------------------------------------------------------------- 6. summary
step "Done"
trap - ERR
if [ "$GPU" != on ]; then ok "stamp $NAME is enrolled without GPUs"
elif [ "$GPU_RUNTIME_OK" -eq 1 ]; then ok "stamp $NAME is enrolled and ready for deployments ($GPUS GPU(s))"
else fail "stamp $NAME is enrolled, but no GPU is allocatable yet (see step 3)"; fi
if [ -n "$CONSOLE_URL" ]; then
    STAMP_PAGE="$CONSOLE_URL/stamps/$STAMP_ID"
else
    STAMP_PAGE="console → Inference stamps → $NAME (/stamps/$STAMP_ID)"
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
