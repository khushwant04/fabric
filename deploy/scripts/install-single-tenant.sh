#!/usr/bin/env bash
# Install one control plane or one stamp with explicit cluster and values inputs.
set -Eeuo pipefail

usage() {
  cat <<'USAGE'
Usage: install-single-tenant.sh control-plane|stamp --context NAME --values FILE
       [--release NAME] [--namespace NAME] [--timeout 15m] [--check]

The values file supplies image references, public URLs, and existing Secret names.
Create its referenced Secrets before installation. --check renders and validates
the chart without changing cluster resources. Secret contents are never printed.
USAGE
}

mode=${1:-}
if [[ "$mode" == --help || "$mode" == -h ]]; then usage; exit 0; fi
if [[ "$mode" != control-plane && "$mode" != stamp ]]; then usage >&2; exit 2; fi
shift
context=""
values=""
release=""
namespace=""
timeout=15m
check=0
while (( $# )); do
  case "$1" in
    --context|--values|--release|--namespace|--timeout)
      (( $# >= 2 )) || { usage >&2; exit 2; }
      case "$1" in
        --context) context=$2 ;;
        --values) values=$2 ;;
        --release) release=$2 ;;
        --namespace) namespace=$2 ;;
        --timeout) timeout=$2 ;;
      esac
      shift 2 ;;
    --check) check=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ -n "$context" && -r "$values" ]] || { usage >&2; exit 2; }
for tool in helm kubectl; do
  command -v "$tool" >/dev/null || { printf '%s is required\n' "$tool" >&2; exit 2; }
done

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
if [[ "$mode" == control-plane ]]; then
  chart="$repo_root/deploy/helm/fabric-control-plane"
  release=${release:-cp}
  namespace=${namespace:-fabric-control}
  policy=(--set singleTenant.enabled=true)
else
  chart="$repo_root/deploy/helm/fabric-stamp"
  release=${release:-stamp}
  namespace=${namespace:-fabric-stamp}
  policy=(--set operator.enabled=true --set stamp.measureCapacity=true)
fi

# Keep rendered Secret manifests and Helm output private even on a failed install.
umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
kubectl --context "$context" cluster-info >"$work/cluster.log" 2>&1 || {
  printf 'Cannot reach context %s. Check its kubeconfig and authentication.\n' "$context" >&2
  exit 1
}
helm lint "$chart" --strict --values "$values" "${policy[@]}" >"$work/lint.log" 2>&1 || {
  cat "$work/lint.log" >&2
  exit 1
}
helm template "$release" "$chart" --namespace "$namespace" \
  --values "$values" "${policy[@]}" >"$work/rendered.yaml"
printf 'Chart configuration validated for %s in context %s, namespace %s.\n' "$mode" "$context" "$namespace"
if (( check )); then exit 0; fi

if ! helm upgrade --install "$release" "$chart" \
  --kube-context "$context" --namespace "$namespace" --create-namespace \
  --values "$values" "${policy[@]}" --atomic --wait --timeout "$timeout" \
  >"$work/install.log" 2>&1; then
  cat "$work/install.log" >&2
  exit 1
fi
printf 'Release %s installed.\n' "$release"
kubectl --context "$context" --namespace "$namespace" get deployments,statefulsets,pods \
  --selector "app.kubernetes.io/instance=$release"
