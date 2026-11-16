# Model-host status and logs

A customer who has just created a deployment wants to watch it come up: queued for
placement, scheduled, pulling the model runtime image, downloading weights, loading them,
compiling and warming up, ready. When a step fails they want the reason and what to do,
without running `kubectl` against the stamp. Creating a deployment in the console opens its
**Logs** tab, which shows exactly that for every model-host pod on every stamp the deployment
is placed on.

## How the data gets there

The control plane never connects to a stamp ([ADR 0001](context/adrs/0001-separate-control-and-data-planes.md),
[ADR 0007](context/adrs/0007-cluster-agent-and-central-telemetry.md)), so the stamp's agent
pushes it:

```
model-host pods ──(API server: pods, events, pods/log)──> fabric-agent
fabric-agent ──POST /v1/stamps/{stamp_id}/hosts (agent credential)──> control plane
console ──GET /v1/accounts/{account_id}/deployments/{deployment_id}/hosts──> control plane
```

**Agent** (`agent/internal/hostreport`). With `--report-hosts`, the agent lists the pods the
operator created in its own namespace
(`app.kubernetes.io/managed-by=fabric-operator,fabric.khushwant.dev/deployment-id`) and, for
each, collects:

- phase, the Ready condition, the `model-host` container's state, reason, message and
  restart count, and the pod conditions;
- the 20 most recent Kubernetes events about the pod (type, reason, message, count, time);
- the last 200 log lines of the `model-host` container. A container that has not started
  has no log; a crash-looping one is read with `previous=true`, which is the instance with
  the traceback.

Logs are cleaned before they leave the cluster: terminal escapes are removed, a
carriage-return progress bar keeps only its final state, each line is capped at 2,000
bytes, and the tail is cut on a line boundary to 48 KiB per pod (and to 640 KiB for the
whole report, so a stamp with many hosts sends shorter tails). Nothing secret is expected in
a vLLM log, but any line matching an obvious credential pattern (`hf_…` tokens, `Bearer …`,
`Authorization:`, `sk-…`, AWS access keys, `api_key=`/`password=`-style assignments, Fabric
credentials, PEM private keys) is replaced whole with
`[line removed: matched a credential pattern]`.

The agent reports every 3 s while any host is not ready or is terminating, and every 30 s
once all are ready (`--host-report-interval`, `--host-report-steady-interval`). A report
runs on its own goroutine and client, so a slow upload never delays reconciliation. A failed
pod listing sends nothing, because an empty report means "every host is gone". A control
plane without the endpoint (404) is retried every 5 minutes.

**Control plane** (`control-plane/app/services/host_reports.py`). Each report is the
complete set of model-host pods on the stamp. The control plane keeps only the latest
snapshot per (deployment, stamp, pod) in `deployment_host_snapshots`: each report
overwrites its pods' rows and deletes the rows of pods it no longer names, so the table
never grows with history. A deployment that is not placed on the reporting stamp (by the
same ownership rule as status reports: a BYOI stamp only for its own account) or that has
been deleted is ignored and listed in `ignored_deployments`; it does not fail the rest of
the report. Deleting a deployment deletes its snapshots, and removing a placement cascades.

The endpoint uses the agent credential and the `stamp:status:write` scope that every agent
credential already holds, so existing stamps need no new credential. Bounds: 64 pods per
report, 50 events per pod, 64 Ki characters of log per pod; unknown fields are rejected.

Snapshots are tenant data. Rows carry the owning account, taken from the placement and never
from the report, and the table has the same forced row-level security policy as every other
account table. The read endpoint requires `deployments:read` for the deployment's account;
it is not cached and answers `Cache-Control: no-store`. A snapshot no report has refreshed
for 2 minutes is marked `stale`; after 15 minutes it is no longer returned.

**Console** (`v1/components/console/deployment-logs.tsx`, logic in
`v1/lib/fabric/host-status.ts`). The Logs tab renders, per pod:

- A stage timeline: *Queued for placement → Scheduled → Pulling model runtime image →
  Downloading weights → Loading weights → Compiling / warming up → Ready*. Stages are derived
  from container state, pod events and vLLM's own start-up lines (`Loading safetensors
  checkpoint shards`, `Loading weights took`, `torch.compile`, `Capturing CUDA graphs`, ...),
  so a step whose event aged out of Kubernetes still reads as done.
- The image pull with elapsed time while it runs, and its size and duration once the kubelet
  reports `Successfully pulled image ... in 4m34s ... Image size: N bytes`. While the image
  is pulling, a note explains that the first deployment on a machine downloads the model
  runtime once; pre-pull is off by default ([model-host-prepull.md](model-host-prepull.md)).
- Weight download progress (below) as bytes and a bar, or elapsed time when it cannot be
  measured.
- Failures in red at the stage they happened, with a diagnosis and a hint: "Failed to infer
  device type" → GPU runtime missing (re-run the bootstrap), CUDA out of memory → lower
  `max_model_len`, `ImagePullBackOff`, `FailedScheduling` for want of a GPU, gated Hugging
  Face repositories, `OOMKilled`, a full disk.
- A full-height raw log viewer: follows new lines until you scroll up, **Pause** freezes it
  (showing how many lines arrived meanwhile), search highlights matches, *Warnings & errors*
  filters lines, and **Download** saves the tail as a file.
- The pod's Kubernetes events, opened automatically when one is a warning.

Before any pod exists the tab shows *Queued for placement*, naming the stamp it waits on, or
*Stamp removed* with a **Redeploy** action. The tab polls the console's own route handler
(`/api/deployments/{id}/hosts`, which holds the control token server-side) every 2.5 s while
any host is not ready and every 15 s afterwards, and pauses while the browser tab is hidden.

## Weight download progress

vLLM prints nothing useful about its download: the Hugging Face progress bars are
carriage-return redraws that a container log keeps only in fragments, and their format
changes with the downloader backend. So the agent measures the result instead of parsing the
log (`agent/internal/weights`):

- The chart mounts the node's model cache (the same `managedModelHost.cache.hostPath` the
  operator gives model hosts) **read-only** into the agent and passes `--model-cache-dir`.
- For a host that is not ready, on the agent's own node (or any node, with a shared
  ReadWriteMany cache and `--model-cache-shared`), the agent sums the completed weight files
  under `models--<org>--<name>/snapshots/*/` and the `blobs/*.incomplete` files being written.
- It compares that with the size of the repository's top-level `*.safetensors` files (or
  `*.bin` when there are none), which is what vLLM loads, from
  `GET https://huggingface.co/api/models/<repo>?blobs=true`. The size is fetched once per
  repository; the agent sends no Hugging Face token.
- The result travels in the host report as `weights` (`downloaded_bytes`, `total_bytes`,
  `in_progress_files`).

Why this rather than log parsing: it is byte-accurate, independent of the vLLM version and of
`hf_transfer`/`hf_xet`, and also shows a warm cache as already complete. It degrades instead
of guessing: a gated or private repository (the API answers 401) reports bytes without a
total; a pod on another node of a multi-node hostPath stamp, a local model path, or
`cache.mode=none` report nothing, and the console shows elapsed time with "progress not
measurable on this stamp". Turn it off with `agent.weightProgress=false`. A control plane
older than this field rejects it; the agent notices the 422 and stops sending it.

## Enabling it

The `fabric-stamp` chart turns it on when the operator manages model hosts
(`operator.enabled` and `operator.managedModelHost.image`); `agent.reportHosts: false` turns
it off. It grants the agent a namespaced Role with read only:

| Resource | Verbs |
|---|---|
| `pods` | `get`, `list` |
| `pods/log` | `get` |
| `events` | `list` |

No ClusterRole, no `exec`/`attach`, no secrets. Outside the chart, run the agent with
`--report-hosts` (or `FABRIC_AGENT_REPORT_HOSTS=true`) and grant the same Role in the stamp
namespace. Without the grant the agent logs the refusal once and keeps serving.

## API

```
POST /v1/stamps/{stamp_id}/hosts            agent credential, stamp:status:write
GET  /v1/accounts/{account_id}/deployments/{deployment_id}/hosts   deployments:read
```

The stamp's own activity log (enrolled, GPUs measured, GPU runtime, image pulls, failures) is
described in [stamp-activity.md](stamp-activity.md).

The request and response schemas are in `control-plane/openapi.json`
(`HostReportRequest`, `DeploymentHostsResponse`).
