#!/usr/bin/env bash
# One command to turn an Ubuntu GPU VM into a Fabric inference stamp.
#
#   curl -fsSL https://raw.githubusercontent.com/khushwant04/fabric/main/scripts/bootstrap-stamp.sh \
#     | sudo bash -s -- --name gpu-01 --control-plane https://api.example.com --token '<token>' \
#         --host gpu-01.stamps.example.com --email ops@example.com
#
# It installs k3s with NVIDIA support, cert-manager and a Let's Encrypt issuer (when --host is
# given), stores the single-use enrollment token in a Secret, installs the stamp chart and
# images of one pinned release, and waits until the agent has enrolled. Re-running it is safe:
# it upgrades the stamp in place and keeps the agent's identity, so no new token is needed.
set -Eeuo pipefail
umask 077

REPOSITORY=https://github.com/khushwant04/fabric
# Chart and images come from the same commit. CI publishes images for every commit on main.
DEFAULT_RELEASE=b5bfb1d4404909cea2c2e705471243812eab8b01
DEFAULT_REGISTRY=public.ecr.aws/hexelstudio/fabric
CERT_MANAGER_VERSION=v1.17.2
NAMESPACE=fabric-stamp
SECRET=stamp-enrollment
ISSUER=fabric-letsencrypt
KUBECONFIG_PATH=/etc/rancher/k3s/k3s.yaml
WORK_ROOT=/opt/fabric

NAME="" CONTROL_PLANE="" ISSUER_URL="" TOKEN="${FABRIC_ENROLLMENT_TOKEN:-}" HOST="" EMAIL=""
REGION="" RELEASE="$DEFAULT_RELEASE" REGISTRY="$DEFAULT_REGISTRY" GPU=auto DTYPE="" PREPULL=""
DRY_RUN=0

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
  --prepull MODELS        comma-separated Hugging Face repositories to cache on the node
  --dtype bfloat16|float16  model dtype; default: bfloat16 on compute capability 8.0+, else float16
  --no-gpu                install without NVIDIA support (control and testing only)
  --issuer URL            JWT issuer, when it differs from --control-plane
  --release SHA           Fabric commit whose chart and images to install (default: pinned)
  --registry REGISTRY     image registry (default: public.ecr.aws/hexelstudio/fabric)
  --dry-run               print the plan and the Helm command, change nothing

Requirements: Ubuntu 22.04/24.04 on x86_64 with systemd; for GPUs, a working NVIDIA driver
(nvidia-smi). Outbound HTTPS to the control plane, GitHub, the registry and Hugging Face.
EOF
}

log() { printf '[fabric-stamp] %s\n' "$*"; }
die() { printf '[fabric-stamp] ERROR: %s\n' "$*" >&2; exit 1; }
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
[[ "$RELEASE" =~ ^[0-9a-f]{40}$ ]] || die '--release must be a full 40-character commit SHA'
[ -z "$REGION" ] || [[ "$REGION" =~ ^[a-z0-9-]{1,63}$ ]] || die '--region must be lowercase letters, digits and hyphens'
[ -z "$DTYPE" ] || [[ "$DTYPE" =~ ^(bfloat16|float16)$ ]] || die '--dtype must be bfloat16 or float16'
[ -z "$PREPULL" ] || [[ "$PREPULL" =~ ^[A-Za-z0-9._/-]+(,[A-Za-z0-9._/-]+)*$ ]] \
    || die '--prepull must be comma-separated repositories such as Qwen/Qwen3.5-4B'
[ -z "$TOKEN" ] || [[ "$TOKEN" != *[[:space:]]* ]] || die 'the enrollment token must not contain whitespace'

if [ "$GPU" = auto ]; then
    if command -v nvidia-smi >/dev/null 2>&1; then GPU=on; else GPU=off; fi
fi
if [ "$GPU" = on ]; then
    GPU_REPORT=$(timeout 20s nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader 2>/dev/null) \
        || die 'nvidia-smi failed; repair the NVIDIA driver first, or pass --no-gpu'
    [ -n "$GPU_REPORT" ] || die 'nvidia-smi reports no GPUs'
    if [ -z "$DTYPE" ]; then
        # T4 and V100 (compute 7.x) have no BF16; Ampere and newer do.
        MIN_MAJOR=$(printf '%s\n' "$GPU_REPORT" | awk -F', *' '{split($2, v, "."); print v[1]}' | sort -n | head -1)
        if [ "${MIN_MAJOR:-0}" -ge 8 ]; then DTYPE=bfloat16; else DTYPE=float16; fi
    fi
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
    HELM_ARGS+=(--set-string gpu.runtimeClassName=nvidia --set-string gpu.nodeSelector.accelerator=nvidia)
fi
if [ -n "$PREPULL" ]; then
    HELM_ARGS+=(--set "operator.managedModelHost.prepull.models={$PREPULL}")
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

log "stamp $NAME -> $CONTROL_PLANE"
log "release ${RELEASE:0:12} images $REGISTRY/*:$TAG"
log "GPU support: $GPU${GPU_REPORT:+ ($(printf '%s' "$GPU_REPORT" | head -1)), dtype $DTYPE}"
if [ -n "$HOST" ]; then log "public endpoint: https://$HOST"; else log "public endpoint: none (add --host for edge routing)"; fi
if [ "$DRY_RUN" -eq 1 ]; then
    printf 'helm upgrade --install %q <chart:%s/deploy/helm/fabric-stamp>' "$NAME" "${RELEASE:0:12}"
    printf ' %q' "${HELM_ARGS[@]}"
    printf '\n'
    exit 0
fi

# --------------------------------------------------------------------------- host setup
[ "$(id -u)" -eq 0 ] || die 'run with sudo'
for tool in curl tar; do command -v "$tool" >/dev/null 2>&1 || die "$tool is required"; done

RELEASE_DIR="$WORK_ROOT/releases/$RELEASE"
if [ ! -f "$RELEASE_DIR/deploy/helm/fabric-stamp/Chart.yaml" ]; then
    log "downloading release ${RELEASE:0:12}"
    mkdir -p "$WORK_ROOT/releases"
    ARCHIVE=$(mktemp "$WORK_ROOT/release.XXXXXX.tar.gz")
    curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 3 \
        "$REPOSITORY/archive/$RELEASE.tar.gz" -o "$ARCHIVE"
    STAGE=$(mktemp -d "$WORK_ROOT/stage.XXXXXX")
    tar -xzf "$ARCHIVE" -C "$STAGE" --strip-components=1
    rm -rf "$RELEASE_DIR" && mv "$STAGE" "$RELEASE_DIR" && rm -f "$ARCHIVE"
fi

K3S_ARGS=()
# Name the node only on a fresh install: an existing server keeps its node name.
systemctl cat k3s.service >/dev/null 2>&1 || K3S_ARGS+=(--node-name "$NAME")
[ "$GPU" = on ] && K3S_ARGS+=(--nvidia)
log "preparing k3s${K3S_ARGS[*]:+ (${K3S_ARGS[*]})}"
bash "$RELEASE_DIR/scripts/bootstrap-k3s.sh" "${K3S_ARGS[@]}"

KUBECTL=(k3s kubectl --kubeconfig "$KUBECONFIG_PATH")
HELM=(helm --kubeconfig "$KUBECONFIG_PATH" --kube-context default)

# --------------------------------------------------------------------------- TLS
if [ -n "$HOST" ]; then
    RESOLVED=$(getent ahostsv4 "$HOST" 2>/dev/null | awk 'NR == 1 {print $1}' || true)
    [ -n "$RESOLVED" ] || log "warning: $HOST does not resolve yet; the certificate is issued once DNS points here"
    if ! "${KUBECTL[@]}" get crd clusterissuers.cert-manager.io >/dev/null 2>&1; then
        log "installing cert-manager $CERT_MANAGER_VERSION"
        "${HELM[@]}" upgrade --install cert-manager cert-manager \
            --repo https://charts.jetstack.io --version "$CERT_MANAGER_VERSION" \
            --namespace cert-manager --create-namespace --set crds.enabled=true --wait --timeout 10m
    fi
    "${KUBECTL[@]}" apply -f - <<EOF
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
fi

# --------------------------------------------------------------------------- enrollment
"${KUBECTL[@]}" get namespace "$NAMESPACE" >/dev/null 2>&1 || "${KUBECTL[@]}" create namespace "$NAMESPACE" >/dev/null
INSTALLED=0
"${HELM[@]}" status "$NAME" --namespace "$NAMESPACE" >/dev/null 2>&1 && INSTALLED=1
if [ -n "$TOKEN" ]; then
    # Piped, never on a command line, so the token stays out of process listings.
    printf '%s' "$TOKEN" | "${KUBECTL[@]}" -n "$NAMESPACE" create secret generic "$SECRET" \
        --from-file=enrollment-token=/dev/stdin --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f - >/dev/null
    TOKEN=""
elif ! "${KUBECTL[@]}" -n "$NAMESPACE" get secret "$SECRET" >/dev/null 2>&1 && [ "$INSTALLED" -eq 0 ]; then
    die '--token is required for a first install (console -> Inference stamps -> Enroll stamp)'
fi

# --------------------------------------------------------------------------- the stamp
log "installing the stamp (this pulls the agent and gateway images)"
"${HELM[@]}" upgrade --install "$NAME" "$RELEASE_DIR/deploy/helm/fabric-stamp" \
    "${HELM_ARGS[@]}" --wait --timeout 15m

log "waiting for the agent to enroll"
ENROLLED=0
for _ in $(seq 1 60); do
    if "${KUBECTL[@]}" -n "$NAMESPACE" logs "statefulset/$NAME-fabric-stamp" -c agent --tail=200 2>/dev/null \
        | grep -qE '"msg":"(enrolled|using persisted stamp identity)"'; then ENROLLED=1; break; fi
    sleep 5
done
[ "$ENROLLED" -eq 1 ] || die "the agent did not enroll; check: k3s kubectl -n $NAMESPACE logs statefulset/$NAME-fabric-stamp -c agent"

if [ -n "$HOST" ]; then
    log "waiting for the TLS certificate for $HOST"
    "${KUBECTL[@]}" -n "$NAMESPACE" wait --for=condition=Ready certificate --all --timeout=300s >/dev/null 2>&1 \
        || log "warning: certificate not ready yet; check DNS for $HOST and that ports 80/443 are open"
fi

GPUS=$("${KUBECTL[@]}" get nodes -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' | awk '{s += $1} END {print s + 0}')
cat <<EOF

[fabric-stamp] Stamp $NAME is enrolled with $CONTROL_PLANE ($GPUS GPU(s) allocatable).
  Deploy a model from the console (Deployments -> Create), choosing infrastructure "$NAME".
  The first deployment on this node pulls the model-host image once (several GB).
  Status:  sudo k3s kubectl -n $NAMESPACE get pods
  Re-run this command at any time to upgrade or repair the stamp; no new token is needed.
EOF
