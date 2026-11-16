# Model-host status and logs

An operator watching a new deployment wants to see its model host go from *pulling the
image* to *container started* to *loading weights* to *ready*, and read the end of the
server's log when a step fails, without running `kubectl` against the stamp. The
deployment detail page's **Hosts** tab shows exactly that, for every model-host pod on
every stamp the deployment is placed on.

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

**Console** (`v1/components/console/deployment-hosts.tsx`). The Hosts tab renders one panel
per pod: a status line, a stage timeline (Scheduled → Pulling image → Image pulled →
Container started → Loading weights → Ready) derived from container state and events, the
recent events with warnings in red, and a monospaced log pane that follows new lines until
you scroll up, with **Jump to latest** to resume. Failures such as `ImagePullBackOff`,
`CrashLoopBackOff`, `OOMKilled` or `FailedScheduling` mark the stage they happened at in red;
failing start-up probes are counted beside *Loading weights*. The tab polls the console's own
route handler (`/api/deployments/{id}/hosts`, which holds the control token server-side)
every 2.5 s while any host is not ready and every 15 s afterwards, and pauses while the
browser tab is hidden.

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

The request and response schemas are in `control-plane/openapi.json`
(`HostReportRequest`, `DeploymentHostsResponse`).
