# Observability

Metrics were collected and discarded before this existed, because there was nowhere to
put them. Prometheus now scrapes three sources and Grafana reads all of them:

| Source | What it answers |
|---|---|
| Model hosts (`vllm:*`) | Engine behaviour: tokens generated, time to first token, time per output token, queue depth, KV cache use |
| Gateway (`fabric_dp_*`) | What the engine cannot see: requests refused for auth, ownership, or limits; end-to-end latency as the caller experiences it |
| DCGM exporter (`DCGM_FI_*`) | The physical GPUs: utilisation, memory, temperature |

The two application sources are deliberately separate. A request refused for an expired
token or a concurrency cap never reaches a model host, so from the engine's side that
traffic did not happen while from the caller's side it happened and failed.

Every series carries `deployment_id`, so two hosts serving the same model can be compared
directly rather than aggregated into one number that describes neither.

## Install

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm upgrade --install obs prometheus-community/kube-prometheus-stack \
  --namespace fabric-observability --create-namespace \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false

helm repo add gpu-helm-charts https://nvidia.github.io/dcgm-exporter/helm-charts
helm upgrade --install dcgm gpu-helm-charts/dcgm-exporter \
  --namespace fabric-observability \
  --set serviceMonitor.enabled=true \
  --set-json 'tolerations=[{"key":"sku","operator":"Equal","value":"gpu","effect":"NoSchedule"}]' \
  --set-json 'nodeSelector={"fabric.khushwant.dev/gpu":"t4"}'
```

The GPU exporter is pinned to GPU nodes. Left unpinned it lands on every node and
crash-loops wherever there is no GPU to read.

Scraping is enabled on the stamp with `--set monitoring.enabled=true`, which renders the
ServiceMonitors. They select hosts by label rather than by name, because the operator
creates and removes hosts as deployments are placed and withdrawn.

The dashboard is loaded by labelling a ConfigMap so Grafana's sidecar picks it up:

```bash
kubectl -n fabric-observability create configmap fabric-dashboard \
  --from-file=fabric-inference.json=deploy/observability/dashboards/fabric-inference.json
kubectl -n fabric-observability label configmap fabric-dashboard grafana_dashboard=1
```
