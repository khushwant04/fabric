#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
base=(--set controlPlane.url=https://control.example.com --set enrollment.existingSecret=enroll --set operator.enabled=true --set operator.managedModelHost.image=vllm:0.26 --set operator.managedModelHost.modelRef=Qwen/Qwen3.5-2B --set operator.managedModelHost.servedName=Qwen/Qwen3.5-2B)
helm template gpu "$ROOT/deploy/helm/fabric-stamp" "${base[@]}" --set operator.managedModelHost.modelRef= --set operator.managedModelHost.servedName= > "$work/empty-stamp.yaml"
rg -q 'model-host-image=vllm:0.26' "$work/empty-stamp.yaml"
if rg -q -- '--model-host-model=|--model-host-served-name=' "$work/empty-stamp.yaml"; then
  printf '%s\n' 'An empty stamp must not require a model before placement.' >&2; exit 1
fi
helm template gpu "$ROOT/deploy/helm/fabric-stamp" "${base[@]}" --set stamp.name=west --set exposure.enabled=true --set exposure.baseDomain=inference.example.com --set exposure.tls.issuer.name=letsencrypt > "$work/native.yaml"
rg -q 'kind: Ingress' "$work/native.yaml"
rg -q 'kind: Certificate' "$work/native.yaml"
rg -q 'https://west.inference.example.com' "$work/native.yaml"
rg -q 'path: /v1' "$work/native.yaml"
rg -q 'model-host-service-account=gpu-fabric-stamp-model-host' "$work/native.yaml"
rg -q 'value: "https://control.example.com"' "$work/native.yaml"
helm template gpu "$ROOT/deploy/helm/fabric-stamp" "${base[@]}" --set exposure.enabled=true --set exposure.host=gpu.example.com --set exposure.tls.secretName=existing-cert > "$work/existing.yaml"
if rg -q 'kind: Certificate' "$work/existing.yaml"; then
  printf '%s\n' 'Existing TLS Secret must not create a Certificate.' >&2; exit 1
fi
if helm template gpu "$ROOT/deploy/helm/fabric-stamp" "${base[@]}" --set exposure.enabled=true --set exposure.host=gpu.example.com > "$work/invalid.yaml" 2> "$work/invalid.log"; then
  printf '%s\n' 'Missing TLS configuration was accepted.' >&2; exit 1
fi
if helm template gpu "$ROOT/deploy/helm/fabric-stamp" "${base[@]}" --set exposure.enabled=true --set exposure.host=gpu.example.com --set exposure.tls.secretName=existing-cert --set istio.enabled=true > "$work/conflict.yaml" 2> "$work/conflict.log"; then
  printf '%s\n' 'Native and Istio exposure conflict was accepted.' >&2; exit 1
fi
helm template gpu "$ROOT/deploy/helm/fabric-stamp" "${base[@]}" --set istio.enabled=true --set 'istio.hosts={gpu.example.com}' --set istio.tls.credentialName=existing-cert > "$work/istio.yaml"
rg -q 'kind: Gateway' "$work/istio.yaml"
rg -q 'inference-url=https://gpu.example.com' "$work/istio.yaml"
if rg -q 'kind: Ingress' "$work/istio.yaml"; then
  printf '%s\n' 'Istio exposure unexpectedly rendered native Ingress.' >&2; exit 1
fi
printf '%s\n' 'Stamp native TLS, endpoint propagation, and Istio compatibility checks passed.'
