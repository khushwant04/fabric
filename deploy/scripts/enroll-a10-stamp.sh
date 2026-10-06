#!/usr/bin/env bash
# Enroll this machine's k3s cluster as a Fabric stamp. Run after install-k3s-gpu.sh.
# No driver changes, model deployment, builds, chart linting, or identity resets.
set -Eeuo pipefail
umask 077

usage() {
  cat <<'USAGE'
Usage: sudo bash deploy/scripts/enroll-a10-stamp.sh \
  --control-plane-url https://CONTROL_PLANE --stamp-name a10-k3s \
  --token-file /PRIVATE/PATH/enrollment-token \
  [--region REGION] [--issuer HTTPS_URL] [--jwks-url HTTPS_URL] \
  [--inference-host DNS_NAME --cluster-issuer NAME | --tls-secret NAME] \
  [--model-image IMAGE] [--data-plane-image IMAGE] [--timeout 15m]

The token file is required only when the enrollment Secret does not yet exist.
Keep it outside this repository, readable only by its owner. Existing Secrets,
agent identity, and PVCs are retained. Later invocations may omit --token-file.
The target is always /etc/rancher/k3s/k3s.yaml, context default, on this machine.
An inference hostname requires working DNS plus a ready cert-manager ClusterIssuer
or an existing TLS Secret in fabric-stamp. Without these options, the stamp joins
with an internal gateway; configure public HTTPS before using the playground.
USAGE
}

fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
require_argument() { (( $# >= 2 )) && [[ -n "$2" ]] || fail "$1 requires a value"; }

control_plane_url=""
stamp_name=""
token_file=""
region=""
issuer=""
jwks_url=""
inference_host=""
cluster_issuer=""
tls_secret=""
model_image=""
data_plane_image=""
timeout=15m
region_supplied=0
while (( $# )); do
  case "$1" in
    --control-plane-url|--stamp-name|--token-file|--region|--issuer|--jwks-url|--inference-host|--cluster-issuer|--tls-secret|--model-image|--data-plane-image|--timeout)
      require_argument "$@"
      case "$1" in
        --control-plane-url) control_plane_url=${2%/} ;;
        --stamp-name) stamp_name=$2 ;;
        --token-file) token_file=$2 ;;
        --region) region=$2; region_supplied=1 ;;
        --issuer) issuer=${2%/} ;;
        --jwks-url) jwks_url=$2 ;;
        --inference-host) inference_host=$2 ;;
        --cluster-issuer) cluster_issuer=$2 ;;
        --tls-secret) tls_secret=$2 ;;
        --model-image) model_image=$2 ;;
        --data-plane-image) data_plane_image=$2 ;;
        --timeout) timeout=$2 ;;
      esac
      shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) fail "Unknown option: $1" ;;
  esac
done
[[ "$EUID" -eq 0 ]] || fail 'Run this script with sudo on the A10 k3s host.'
[[ -n "$control_plane_url" && -n "$stamp_name" ]] || { usage >&2; exit 2; }
for tool in k3s helm python3 realpath install flock; do
  command -v "$tool" >/dev/null || fail "$tool is required; run scripts/install-k3s-gpu.sh first."
done
[[ -z "$cluster_issuer" || -z "$tls_secret" ]] || fail 'Choose --cluster-issuer or --tls-secret, not both.'
if [[ -n "$inference_host" ]]; then
  [[ -n "$cluster_issuer" || -n "$tls_secret" ]] || fail '--inference-host requires --cluster-issuer or --tls-secret.'
else
  [[ -z "$cluster_issuer" && -z "$tls_secret" ]] || fail 'TLS configuration requires --inference-host.'
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
chart="$repo_root/deploy/helm/fabric-stamp"
image_values="$repo_root/deploy/releases/singletenant-20261006/stamp-images.yaml"
[[ -f "$chart/Chart.yaml" && -f "$image_values" ]] || fail 'Run this script from a complete Fabric checkout.'
k3s_config=/etc/rancher/k3s/k3s.yaml
[[ -r "$k3s_config" ]] || fail 'Local k3s kubeconfig is missing; run scripts/install-k3s-gpu.sh first.'
kubectl=(k3s kubectl --kubeconfig "$k3s_config" --context default)
helm_local=(helm --kubeconfig "$k3s_config" --kube-context default)
release=a10-stamp
namespace=fabric-stamp
fullname=fabric-a10
enrollment_secret=fabric-stamp-enrollment
state_dir=/etc/fabric
values_path="$state_dir/a10-stamp-values.json"
default_model_image='vllm/vllm-openai@sha256:d8d39b59e909d2378ac4feeb191f7e7b6f1342477dc66b7c47cec89e9985ad8a'

[[ ! -L "$state_dir" ]] || fail 'Refusing a symlinked Fabric configuration directory.'
[[ ! -e "$state_dir" || -d "$state_dir" ]] || fail 'Fabric configuration path must be a directory.'
install -d -m 700 "$state_dir"
[[ ! -L "$state_dir/a10-stamp-enrollment.lock" && ! -L "$values_path" ]] || fail 'Refusing symlinked enrollment state files.'
exec 9>"$state_dir/a10-stamp-enrollment.lock"
flock -n 9 || fail 'Another A10 stamp enrollment is in progress.'
work=$(mktemp -d "$state_dir/enroll.XXXXXXXX")
trap 'rm -rf "$work"' EXIT

# Validate arguments without putting a token in the Python argv or shell variables.
python3 - "$control_plane_url" "$stamp_name" "$issuer" "$jwks_url" "$inference_host" \
  "$cluster_issuer" "$tls_secret" "$model_image" "$data_plane_image" "$timeout" "$region" <<'PY'
import re
import sys
from urllib.parse import urlsplit

cp, name, issuer, jwks, host, cluster_issuer, tls_secret, image, data_plane_image, timeout, region = sys.argv[1:]
def reject(message):
    raise SystemExit(f"Error: {message}")
for value, label in ((cp, "Control-plane URL"), (issuer, "Issuer URL"), (jwks, "JWKS URL")):
    if not value:
        continue
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
        reject(f"{label} must be an HTTPS URL without credentials, query, or fragment.")
    try:
        parsed.port
    except ValueError:
        reject(f"{label} has an invalid port.")
dns_label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
if not re.fullmatch(dns_label, name):
    reject("Stamp name must be a lowercase DNS label, at most 63 characters.")
for value, label in ((host, "Inference hostname"), (cluster_issuer, "ClusterIssuer name"), (tls_secret, "TLS Secret name")):
    if value and (len(value) > 253 or not re.fullmatch(dns_label + r"(?:\." + dns_label + r")*", value)):
        reject(f"{label} must be a lowercase DNS name.")
if host and "." not in host:
    reject("Inference hostname must include a domain.")
for value, label in ((image, "Model image"), (data_plane_image, "Data-plane image")):
    if value and (value.startswith("-") or any(c.isspace() for c in value) or len(value) > 1024):
        reject(f"{label} must be a container image reference without whitespace.")
if data_plane_image:
    if "@" in data_plane_image:
        repository, digest = data_plane_image.rsplit("@", 1)
        if not repository or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            reject("Data-plane image digest must be a complete repository@sha256 reference.")
    else:
        last_component = data_plane_image.rsplit("/", 1)[-1]
        if ":" not in last_component or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", last_component.rsplit(":", 1)[1]):
            reject("Data-plane image must include an explicit tag or sha256 digest.")
if not re.fullmatch(r"[1-9][0-9]*(?:s|m|h)", timeout):
    reject("Timeout must be a positive duration such as 120s, 15m, or 1h.")
if len(region) > 128 or any(ord(c) < 32 for c in region):
    reject("Region must be a short single-line value.")
PY

local_server=$("${kubectl[@]}" config view --minify --output 'jsonpath={.clusters[0].cluster.server}')
[[ "$local_server" == https://127.0.0.1:6443 || "$local_server" == https://localhost:6443 ]] || \
  fail 'The local k3s kubeconfig must target the loopback API server on port 6443.'
"${kubectl[@]}" get nodes --output json >"$work/nodes.json"
python3 - "$work/nodes.json" <<'PY'
import json
import sys

nodes = json.load(open(sys.argv[1]))["items"]
if len(nodes) != 1:
    raise SystemExit("Error: This enrollment workflow expects one k3s node with both A10 GPUs.")
node = nodes[0]
ready = any(c["type"] == "Ready" and c["status"] == "True" for c in node.get("status", {}).get("conditions", []))
labels = node.get("metadata", {}).get("labels", {})
gpu_count = int(node.get("status", {}).get("allocatable", {}).get("nvidia.com/gpu", "0"))
if not ready or node.get("spec", {}).get("unschedulable", False):
    raise SystemExit("Error: The local k3s node must be Ready and schedulable.")
if labels.get("accelerator") != "nvidia" or gpu_count < 2:
    raise SystemExit("Error: The node must advertise at least two NVIDIA GPUs; rerun scripts/install-k3s-gpu.sh.")
print(f"Local k3s node {node['metadata']['name']} is Ready and advertises {gpu_count} GPUs.")
PY
"${kubectl[@]}" get runtimeclass nvidia --output name >/dev/null || fail 'NVIDIA RuntimeClass is missing.'
"${kubectl[@]}" get storageclass local-path --output name >/dev/null || fail 'The local-path storage class is required for persistent stamp identity.'

"${helm_local[@]}" list --all --namespace "$namespace" --filter "^${release}$" --output json >"$work/releases.json"
if python3 - "$work/releases.json" <<'PY'
import json
import sys
sys.exit(0 if json.load(open(sys.argv[1])) else 1)
PY
then
  "${helm_local[@]}" get values "$release" --namespace "$namespace" --output json >"$work/existing-values.json"
elif [[ -f "$values_path" ]]; then
  # Also guards identity after an atomic first-install rollback retained its PVCs.
  cp "$values_path" "$work/existing-values.json"
else
  printf '{}\n' >"$work/existing-values.json"
fi

# JSON is accepted as a Helm values file. Existing settings remain unless an option
# explicitly replaces them. A different control plane or stamp name requires an
# intentional identity migration rather than silently reusing stored credentials.
python3 - "$work/existing-values.json" "$work/values.json" "$control_plane_url" "$stamp_name" \
  "$issuer" "$jwks_url" "$region" "$region_supplied" "$inference_host" "$cluster_issuer" \
  "$tls_secret" "$model_image" "$default_model_image" "$fullname" "$enrollment_secret" "$data_plane_image" <<'PY'
import copy
import json
import sys

(existing_path, output_path, cp, name, issuer, jwks, region, region_supplied, host,
 cluster_issuer, tls_secret, image, default_image, fullname, enrollment_secret, data_plane_image) = sys.argv[1:]
existing = json.load(open(existing_path)) or {}
old_cp = existing.get("controlPlane", {}).get("url", "").rstrip("/")
old_name = existing.get("stamp", {}).get("name", "")
if existing and (old_cp != cp or old_name != name):
    raise SystemExit("Error: Existing stamp identity belongs to a different control plane or stamp name. Its identity was preserved.")
if existing and existing.get("fullnameOverride", fullname) != fullname:
    raise SystemExit("Error: Existing stamp uses a different workload name. Its identity was preserved.")
old_issuer = existing.get("controlPlane", {}).get("jwtIssuer", "") or old_cp
if existing and issuer and issuer != old_issuer:
    raise SystemExit("Error: Issuer differs from the existing stamp. Keep the original issuer for this identity.")
values = {
    "fullnameOverride": fullname,
    "controlPlane": {"url": cp, "jwtIssuer": issuer or cp, "jwksUrl": jwks},
    "enrollment": {"token": "", "existingSecret": enrollment_secret, "existingSecretKey": "enrollment-token"},
    "stamp": {"name": name, "orchestrator": "k3s", "region": region, "measureCapacity": True},
    "operator": {"enabled": True, "installCRD": True, "managedModelHost": {
        "image": image or default_image, "dtype": "bfloat16", "gpus": 1,
        "maxModelLen": 4096, "maxNumSeqs": 2, "gpuMemoryUtilization": "0.85", "enforceEager": True,
        "textOnly": False, "textOnlyModels": [],
        # True/empty emits no new flags into the older, published operator binary.
        "toolCalling": {"modelDefaults": True, "modelParsers": {}},
        "kernel": {"blockV": 0, "numWarps": 0},
        "cache": {"mode": "hostPath", "hostPath": "/var/lib/fabric/model-cache", "claim": ""},
    }},
    "gpu": {"nodeSelector": {"accelerator": "nvidia"}, "runtimeClassName": "nvidia", "spreadAcrossNodes": True},
    "persistence": {"enabled": True, "size": "128Mi", "storageClass": "local-path"},
    "usageSpool": {"enabled": True, "size": "256Mi", "storageClass": "local-path"},
    "exposure": {"enabled": False}, "istio": {"enabled": False},
    "service": {"type": "ClusterIP", "port": 80}, "networkPolicy": {"enabled": True},
}
def merge(target, source):
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
merge(values, existing)
values["fullnameOverride"] = fullname
values["controlPlane"]["url"] = cp
if issuer:
    values["controlPlane"]["jwtIssuer"] = issuer
if jwks:
    values["controlPlane"]["jwksUrl"] = jwks
values["stamp"].update({"name": name, "orchestrator": "k3s", "measureCapacity": True})
if region_supplied == "1":
    values["stamp"]["region"] = region
values["enrollment"] = {"token": "", "existingSecret": enrollment_secret, "existingSecretKey": "enrollment-token"}
values["operator"]["enabled"] = True
host_settings = values["operator"]["managedModelHost"]
if image:
    host_settings["image"] = image
host_settings.update({"textOnly": False, "textOnlyModels": [], "toolCalling": {"modelDefaults": True, "modelParsers": {}}, "kernel": {"blockV": 0, "numWarps": 0}})
values["gpu"]["runtimeClassName"] = "nvidia"
values["gpu"]["nodeSelector"] = {"accelerator": "nvidia"}
values["persistence"].update({"enabled": True, "storageClass": "local-path"})
values["usageSpool"].update({"enabled": True, "storageClass": "local-path"})
if host:
    values["istio"]["enabled"] = False
    values["exposure"] = {"enabled": True, "host": host, "className": "traefik", "tls": {
        "secretName": tls_secret or f"{fullname}-inference-tls",
        "issuer": {"name": cluster_issuer, "kind": "ClusterIssuer"},
    }}
if data_plane_image:
    if "@" in data_plane_image:
        repository, digest = data_plane_image.rsplit("@", 1)
        tag = ""
    else:
        repository, tag = data_plane_image.rsplit(":", 1)
        digest = ""
    values.setdefault("image", {})["dataPlane"] = {"repository": repository, "tag": tag, "digest": digest, "pullPolicy": "IfNotPresent"}
with open(output_path, "w") as handle:
    json.dump(values, handle, indent=2)
    handle.write("\n")
PY

"${kubectl[@]}" create namespace "$namespace" --dry-run=client --output yaml | "${kubectl[@]}" apply --filename - >/dev/null
"${kubectl[@]}" --namespace "$namespace" get statefulsets --selector app.kubernetes.io/name=fabric-stamp \
  --output json >"$work/stamps.json"
python3 - "$work/stamps.json" "$fullname" <<'PY'
import json
import sys
if any(item["metadata"]["name"] != sys.argv[2] for item in json.load(open(sys.argv[1]))["items"]):
    raise SystemExit("Error: Another stamp already owns fabric-stamp. Use a separate cluster or an intentional migration.")
PY

read -r exposure_enabled active_host active_issuer active_tls_secret < <(python3 - "$work/values.json" <<'PY'
import json
import sys
exposure = json.load(open(sys.argv[1])).get("exposure", {})
tls = exposure.get("tls", {})
print("true" if exposure.get("enabled") else "false", exposure.get("host") or "-", tls.get("issuer", {}).get("name") or "-", tls.get("secretName") or "-")
PY
)
if [[ "$exposure_enabled" == true ]]; then
  "${kubectl[@]}" get ingressclass traefik --output name >/dev/null || fail 'The traefik IngressClass is required for this k3s gateway.'
  if [[ "$active_issuer" != - ]]; then
    "${kubectl[@]}" wait "clusterissuer/$active_issuer" --for=condition=Ready --timeout=120s >/dev/null || \
      fail 'The cert-manager ClusterIssuer is missing or not Ready; configure it before enrolling with public HTTPS.'
  else
    "${kubectl[@]}" --namespace "$namespace" get secret "$active_tls_secret" --output json >"$work/tls-secret.json"
    python3 - "$work/tls-secret.json" <<'PY'
import json
import sys
secret = json.load(open(sys.argv[1]))
if secret.get("type") != "kubernetes.io/tls" or not all(secret.get("data", {}).get(k) for k in ("tls.crt", "tls.key")):
    raise SystemExit("Error: Existing TLS Secret must have type kubernetes.io/tls and contain tls.crt and tls.key.")
PY
  fi
fi

secret_exists=$("${kubectl[@]}" --namespace "$namespace" get secret "$enrollment_secret" --ignore-not-found --output name)
retained_identity=0
if [[ -z "$secret_exists" ]]; then
  # A consumed token may have been removed. Require a running agent and its Bound
  # claim, rather than assuming that a saved values file means enrollment succeeded.
  "${kubectl[@]}" --namespace "$namespace" get statefulset "$fullname" --ignore-not-found --output json >"$work/identity-workload.json"
  "${kubectl[@]}" --namespace "$namespace" get pvc "agent-state-$fullname-0" --ignore-not-found --output json >"$work/identity-pvc.json"
  if python3 - "$work/identity-workload.json" "$work/identity-pvc.json" <<'PY'
import json
import sys
try:
    workload = json.load(open(sys.argv[1]))
    pvc = json.load(open(sys.argv[2]))
except (json.JSONDecodeError, FileNotFoundError):
    sys.exit(1)
status = workload.get("status", {})
sys.exit(0 if status.get("readyReplicas", 0) >= 1 and pvc.get("status", {}).get("phase") == "Bound" else 1)
PY
  then
    retained_identity=1
  fi
fi
if [[ -n "$secret_exists" ]]; then
  printf 'Existing enrollment Secret preserved; no token file was read.\n'
elif (( retained_identity )); then
  printf 'Running stamp and Bound identity PVC found; consumed enrollment Secret remains absent.\n'
else
  [[ -n "$token_file" ]] || fail 'The first install requires --token-file with a fresh dashboard enrollment token.'
  [[ -f "$token_file" && -r "$token_file" && ! -L "$token_file" ]] || fail 'Token file must be a readable regular file, not a symlink.'
  token_file=$(realpath "$token_file")
  case "$token_file" in "$repo_root"|"$repo_root"/*) fail 'Keep the enrollment token file outside the repository.' ;; esac
  python3 - "$token_file" "$work/enrollment-token" "${SUDO_UID:-0}" <<'PY'
import os
import stat
import sys

source, destination, sudo_uid = sys.argv[1:]
metadata = os.stat(source)
if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
    raise SystemExit("Error: Token file must have private permissions, for example chmod 600.")
if metadata.st_uid not in {0, int(sudo_uid)}:
    raise SystemExit("Error: Token file must belong to root or the invoking sudo user.")
if not 0 < metadata.st_size <= 16384:
    raise SystemExit("Error: Token file is empty or unexpectedly large.")
token = open(source, encoding="utf-8").read().strip()
if not token or any(c.isspace() for c in token):
    raise SystemExit("Error: Token file must contain exactly one enrollment token.")
with open(destination, "w", encoding="utf-8") as handle:
    handle.write(token)
PY
  # The manifest is streamed privately. Neither command output nor errors can expose
  # token data, and the token is never part of Helm values/history or process argv.
  if ! "${kubectl[@]}" --namespace "$namespace" create secret generic "$enrollment_secret" \
    --from-file="enrollment-token=$work/enrollment-token" --dry-run=client --output yaml \
    2>"$work/secret-create.log" | "${kubectl[@]}" --namespace "$namespace" apply --filename - \
    >"$work/secret-apply.log" 2>&1; then
    fail 'Could not create the enrollment Secret; its contents were not printed.'
  fi
  rm -f "$work/enrollment-token"
fi

install -m 600 "$work/values.json" "$values_path"
install -d -m 755 /var/lib/fabric/model-cache
log_dir=/var/log/fabric
[[ ! -L "$log_dir" ]] || fail 'Refusing a symlinked Fabric log directory.'
[[ ! -e "$log_dir" || -d "$log_dir" ]] || fail 'Fabric log path must be a directory.'
install -d -m 700 "$log_dir"
install_log="$log_dir/a10-stamp-install.log"
[[ ! -L "$install_log" ]] || fail 'Refusing a symlinked installation log.'
install -m 600 /dev/null "$install_log"
printf 'Installing Fabric stamp %s using the local k3s API.\n' "$stamp_name"
if ! "${helm_local[@]}" upgrade --install "$release" "$chart" --namespace "$namespace" \
  --values "$image_values" --values "$values_path" --atomic --wait --timeout "$timeout" \
  >"$install_log" 2>&1; then
  fail "Helm installation failed. Private details are in $install_log; enrollment identity and storage were preserved."
fi
"${kubectl[@]}" --namespace "$namespace" get deployments,statefulsets,pods \
  --selector "app.kubernetes.io/instance=$release"
printf 'Stamp installed. Confirm its heartbeat and measured GPUs on the dashboard before deploying a model.\n'
printf 'For one model across both A10s, choose 1 replica and 2 GPUs per replica.\n'
printf 'Default model image is vLLM 0.11.0 with CUDA 12.8.1; use a compatible model such as Qwen2.5-14B-Instruct.\n'
printf 'Qwen3.5 requires a newer driver-compatible model image. Automatic tool flags require a newer operator image.\n'
if [[ "$exposure_enabled" == true ]]; then
  if [[ "$active_issuer" != - ]]; then
    if ! "${kubectl[@]}" --namespace "$namespace" wait "certificate/$fullname-inference" \
      --for=condition=Ready --timeout="$timeout" >/dev/null; then
      fail "Stamp joined, but its HTTPS certificate is pending. Check DNS and cert-manager for $active_host."
    fi
  fi
  printf 'Configured inference URL: https://%s\n' "$active_host"
  printf 'Point DNS at the VM ingress address, allow HTTPS, and approve this gateway in the console.\n'
else
  printf 'Gateway is internal. Add --inference-host plus --cluster-issuer or --tls-secret to enable public HTTPS.\n'
fi
printf 'Private values: %s\n' "$values_path"
