# Packaging and Deployment

**Status:** Implemented for a stamp — container images in [`deploy/images/`](../../deploy/images/) and a Helm chart in [`deploy/helm/fabric-stamp/`](../../deploy/helm/fabric-stamp/) install a working inference stamp on Kubernetes, with or without the operator. Verified on a real cluster by [`deploy/scripts/kind-e2e.sh`](../../deploy/scripts/kind-e2e.sh) in both modes.
**Not included:** the model host image itself. Fabric does not build one; with
`operator.managedModelHost.image` set the operator runs and owns an inference server per
placed deployment, and without it the data plane is configured against an upstream
someone else operates.
**Design reference:** [ADR 0004](adrs/0004-local-operator-and-single-intent-crd.md), [ADR 0007](adrs/0007-cluster-agent-and-central-telemetry.md), [ADR 0008](adrs/0008-account-scoped-tenancy-and-stamp-credentials.md), [Operator and Deployment](operator-deployment.md).

## Images

| Image | Base | Contents |
|---|---|---|
| `fabric/agent` | `gcr.io/distroless/static-debian12:nonroot`, pinned by digest | `fabric-agent` and `fabric-collector`, static, stripped |
| `fabric/data-plane` | `python:3.12-slim-bookworm` | The data-plane package in a virtualenv |
| `fabric/control-plane` | `python:3.12-slim-bookworm` | The control-plane package plus Alembic migrations |

All three run as uid 65532 with no shell in the agent image. The two binaries share
an image because they are versioned together, but they run as separate containers
with separate mounts and separate credentials: one image is not one trust boundary.

Each image holds exactly one copy of its code. An earlier revision both installed
the package and copied the source, which left import resolution depending on the
working directory.

## Stamp topology

A stamp is one StatefulSet, not a Deployment, because the agent holds a durable
identity: enrollment tokens are single use, so credentials that did not survive a
restart would need an operator to issue a new token.

```
+---------------------------- pod ----------------------------+
|                                                             |
|  agent            data-plane              collector         |
|   |                  |                       |              |
|   |  credentials.json (PVC, 0600) -- agent only             |
|   |                  |                       |              |
|   +--> deployments.json (emptyDir) --> read-only mount       |
|   |                  |                       |              |
|   +--> telemetry-credential (memory emptyDir) --> read-only  |
|                      |                       |              |
|                 :8080 inference        drains :8081 over     |
|                 (published)            localhost only       |
+-------------------------------------------------------------+
        |                                       |
        v                                       v
   customer traffic                      control plane (outbound)
```

Three volumes rather than one, because the files have different audiences:

| Volume | Holder | Why it is separate |
|---|---|---|
| `agent-state` (PVC) | agent only | Holds the agent credential. Not mounted into the other containers, so neither can read it. |
| `rendered-config` (emptyDir) | agent writes, data plane reads read-only | Derived state, rewritten every pass, and holds no secret. |
| `collector-secret` (memory emptyDir) | agent writes, collector reads read-only | The collector gets the write-only telemetry credential and nothing else. |

Separation is by mount, not by user id: a container that never mounts a volume
cannot read it regardless of the uid it runs as.

## What running it on Kubernetes changed

Local process tests passed while several deployment-only defects were live. Each is
now covered by a test.

| Defect | Why local runs missed it |
|---|---|
| Data plane silently ignored all configuration | Its environment prefix is `FABRIC_DP_`, and the chart set `FABRIC_`. Unknown variables are ignored, so it ran with defaults pointing at `control.fabric.local` and answered liveness while verifying tokens against the wrong issuer. |
| A pod could never become ready | Readiness requires verification keys, which were only fetched while serving a request, and no request arrives before readiness. The keys are now warmed at startup and retried. |
| Probes could not reach the health endpoint | Health lived only on the administrative listener, which binds to localhost so the drain is unreachable from the cluster. The kubelet probes the pod address, so probes were added to the public listener instead of widening that bind. |
| Readiness reported failure with HTTP 200 | A probe reads the status code, so an unready data plane would have been sent traffic it rejects. It returns 503 now. |
| A placement never reached a running pod | The registry was read once at startup, so a new placement needed a pod restart and a withdrawn one kept being served. The data plane now follows the file the agent rewrites. |
| A restarted stamp served nothing | Credentials persist on a volume, the rendered configuration usually does not. The agent asked for changes after its acknowledged generation, received nothing, and never re-rendered. A missing file now triggers a rebuild from full desired state; an empty file is left alone, because that is how "nothing is placed here" is expressed. |
| A restarted agent starved the collector | The telemetry credential was written only at enrollment, and a restart does not re-enrol. The hand-off now runs on every start. |
| A failed status report was lost permanently | Desired state does not repeat an acknowledged assignment, so the report was never retried and the customer saw no status for a serving deployment. Unaccepted reports are now held and retried. |
| Managed PostgreSQL closed pooled connections | Pre-ping alone leaves a race, so a dropped connection failed mid-statement and surfaced as a 500 to the agent. Connections are now recycled below the usual idle timeout. |
| Every usage row was refused by row-level security | The tenant context was re-applied on savepoints as well as transactions, and usage ingestion opens a savepoint per record, so each insert ran unelevated inside an elevated transaction. Only visible with policies in force *and* a real collector reporting. |
| The migration Job never created a pod | Its ServiceAccount and Secret were ordinary resources, which Helm creates after hooks. |

## Control plane

[`deploy/helm/fabric-control-plane/`](../../deploy/helm/fabric-control-plane/) installs
the control plane. Fabric operates this itself; a customer installing a stamp does not
install it.

| Decision | Reason |
|---|---|
| Migrations run as a `pre-install,pre-upgrade` hook | The schema must be in place before any replica serves, or a pod expects a column that does not exist |
| The Secret and ServiceAccount are hooks too, weighted ahead of the Job | Hooks run before ordinary resources, so a Job referencing them is otherwise rejected with no pod created |
| The signing key is never generated by the chart | A key generated at install time would be replaced on upgrade, invalidating every issued token and every data plane's cached JWKS |
| Secret and ServiceAccount are `resource-policy: keep` | The pepper hashes every stored credential; losing it is unrecoverable rather than inconvenient |
| Liveness probes `/healthz`, readiness probes `/readyz` | `/readyz` checks the database, so using it for liveness would restart every replica during a database blip and turn a recoverable outage into a cold start |
| One uvicorn worker per pod | Replicas are the unit of concurrency; a worker count would multiply connection-pool size per pod invisibly |
| No ServiceAccount token mounted | The control plane calls no Kubernetes API |

The chart refuses to render without a database URL, a signing key, a credential pepper,
a token issuer, and an Auth0 audience, because each omission produces a deployment that
cannot work. The database role must not hold `BYPASSRLS`, or the row-level security
policies are silently inert; the control plane logs that at startup, and
[`control-plane/scripts/create-app-role.sql`](../../control-plane/scripts/create-app-role.sql)
creates a suitable role.

## Operator and CRD

With `operator.enabled=true` the agent stops writing the data plane's file and declares
one `FabricModelDeployment` per assignment instead. The operator reconciles those
declarations into the ConfigMap the data plane mounts, and reports what it observed on
each resource's status. The agent then forwards the operator's verdict to the control
plane rather than its own, so the control plane learns what the cluster did.

The split exists for a specific reason: the agent holds central credentials and the
operator holds Kubernetes permissions, and neither holds both. A compromise of the
agent cannot mutate the cluster beyond declaring intent that the operator will validate;
a compromise of the operator cannot talk to Fabric at all.

| Component | Kubernetes permissions | Fabric credentials |
|---|---|---|
| Agent | create/update/delete `fabricmodeldeployments`, read their status | agent credential, telemetry credential to hand over |
| Operator | read `fabricmodeldeployments`, patch their status, write one named ConfigMap | none |
| Data plane | none (no service-account token mounted) | none; verifies tokens with public keys |
| Collector | none | write-only telemetry credential |

Status is a subresource, so the agent declares intent and cannot write status, while
the operator reports status and cannot change intent. The operator's ConfigMap rule is
restricted by `resourceNames` to the single object it manages.

`Applied` carries a reason that names what actually happened, and it differs by mode:
`DataPlaneConfigurationRendered` when the operator only writes configuration, and
`ModelHostAndConfigurationApplied` when it also runs the server. Neither can imply a
running workload on a stamp where none was started.

With `operator.managedModelHost.image` set, the operator creates a Deployment and a
Service per deployment and overrides the upstream in the rendered configuration, since it
is the only component that knows where the host ended up. Readiness is a separate
`ModelHostReady` condition taken from the workload's own status, never assumed from a
successful write: a model server is minutes from answering after its container starts,
and while it is starting the phase is `pending`.

| Choice | Reason |
|---|---|
| `Recreate` rather than rolling updates | Two replicas would both want the GPU, and the new one would never schedule while the old holds it |
| One replica per deployment | A GPU is not shared; scaling is the control plane placing on more stamps |
| GPU as a limit only | Kubernetes requires request and limit to be equal for extended resources |
| Generous readiness threshold | Weight loading is slow, and a tight probe restarts the pod before it finishes |
| Hosts pruned by label | An operator that restarted would otherwise leak a workload holding a GPU |
| Workload permissions only with an image | An operator that runs no server cannot create workloads either |
| Weights cached on the node's own disk | They are large, immutable, and only useful to a pod already scheduled there; a GPU SKU's ephemeral disk is local NVMe and already paid for |
| `float16` rather than `bfloat16` by default | A T4 is compute capability 7.5 with no bfloat16 support, and a server asked for it refuses to start |
| A startup probe rather than a long liveness delay | A fixed delay plus a failure threshold is a deadline; a cold model compiling graphs on a T4 overran 480 seconds and was killed mid-startup, losing the compilation each time |
| Compiled graphs cached beside the weights | Expensive to produce, identical on every start, and only useful to a pod already on that node |
| A placeholder upstream when the operator owns the host | The operator's Service is named after a deployment ID that does not exist at install time, so an unroutable placeholder fails closed until a deployment is placed |

## Hardware profiling

Several serving settings are properties of the GPU rather than preferences, and getting one
wrong does not make serving slower, it prevents it. A host asked for bfloat16 on a T4 exits
before it listens, because compute capability 7.5 has no bfloat16 at all. That happened
here, from a chart default that was correct on the GPU it was written for.

The operator therefore describes the hardware before it creates anything. It reads the GPU
nodes matching its own placement selector, prefers labels naming the device where GPU
feature discovery provides them, and otherwise infers the device from the machine type,
because compute capability is a property of the chip and the chip is implied by the SKU.
Which of the two it used is recorded, so a profile inferred from a machine type can be told
apart from one measured on the device.

Only settings that cannot work are changed. A merely slower choice is left alone: silently
improving a value would make the platform unpredictable, and the operator who set it may
know something the platform does not. When a value is overridden the reason names the
hardware, at warning level, because changing what somebody asked for without saying so is
its own kind of failure.

Where a pool holds different GPUs, the weakest decides. A host may be scheduled onto any
node in the pool, and a setting that only works on the best of them fails intermittently,
which is harder to diagnose than failing always.

| Observation from the deployed cluster | |
|---|---|
| Profiled | `Tesla T4`, compute capability 7.5, 16384 MiB, 1 GPU per node |
| Source | machine type `Standard_NC4as_T4_v3` |
| Requested | `bfloat16` |
| Applied | `float16`, with the reason recorded |

## Deployed environment

One AKS cluster in `centralindia` runs both planes, in separate namespaces, against an
Azure PostgreSQL server. Measured on that cluster:

| Observation | Value |
|---|---|
| Model load | 4.25 GiB of weights in 3.3 s from the node's own disk |
| Cold start to serving | about 510 s, most of it compiling graphs |
| Startup window allowed | 20 minutes, after 480 s proved too strict |
| Attribution | 19 input and 4 output tokens, matching the server's own count |

The control plane is exposed through the cluster's existing Istio ingress gateway rather
than a Kubernetes Ingress, because a cluster running the Istio addon has no IngressClass:
an Ingress object would be created and never served, and adding a controller to serve it
would put two of them in front of one cluster.

TLS terminates at the gateway from a Kubernetes secret and plaintext is redirected, even
though a CDN in front already terminates TLS for clients. The hop from that CDN to the
cluster crosses the public internet carrying API keys and tokens, so `terminatedUpstream`
exists for cases where that hop is private and is not what a credential-issuing API
should use. Verified end to end: an API key exchanged over the public hostname returns a
token whose issuer is that hostname, and the same hostname serves the JWKS that verifies
it.

The data plane's inference listener is exposed the same way, on its own hostname, and
only that listener: the administrative listener the collector drains is not in the
Service at all, so no route can reach it. Its gateway timeout is deliberately long,
because a generation, or a stream held open while tokens are produced, outlives any
timeout chosen for an ordinary API, and cutting one off mid-answer would bill for work
never received.

Gateway port names are derived from the release. Several Gateway resources bind the same
shared ingress workload and Istio merges the servers declared for a port, so two of them
naming a port identically is a conflict waiting to happen.

Where a CDN proxies these hostnames, its own origin timeout applies on top of the
gateway's. On plans that cap it at 100 seconds, a non-streaming completion longer than
that is cut off by the CDN regardless of what the cluster does, so streaming responses
are the safe default for long generations.

The cluster enforces NetworkPolicy through Cilium and runs Gatekeeper with every
constraint in `dryrun`, so policy reports violations rather than rejecting workloads. The
control plane is reached over in-cluster DNS while its public hostname is pending, but
tokens still carry the public issuer, so the same tokens stay valid once it is exposed.
| GPU scheduling on the host alone | The agent, data plane, and collector need no device, and pinning them to GPU nodes would waste capacity only the server can use |
| Anti-affinity preferred, not required | On a single-node stamp a hard rule leaves the second deployment permanently Pending, which is worse than sharing a node |
| Tolerations default to `Exists` without a value | A taint meaning "this node has a GPU" carries no value, and `Equal` with an empty value would not match it |

### Changing a release

Replacing a model host is not a rolling update. The GPU cannot be shared, so the old
server stops before the new one starts, and the new one then loads weights for minutes
before it can answer. That window is unavoidable; leaving a stamp in it forever is not.

| Behaviour | Reason |
|---|---|
| One host changes release at a time | A bad release costs one deployment rather than the whole stamp |
| A stalled release rolls back to the last one observed ready | Returning to something that worked beats serving nothing until a human intervenes |
| Only a release seen ready becomes the fallback | Recording the declared release optimistically would let a broken one become the thing rolled back to |
| No fallback means keep trying | Rolling back to nothing would remove the only deployment the stamp has |
| The deadline resets only when the release changes | Otherwise every pass grants a fresh deadline and a stalled release never times out |
| History lives in annotations on the workload | An operator restart must not forget which release was good |
| `Progressing` is its own condition | A host that is starting is distinguishable from one that was abandoned and rolled back |

There is no traffic splitting: one GPU serves one server at a time, so a percentage of
traffic cannot be expressed. Progressive here means one deployment at a time across a
stamp.

The CRD is annotated `helm.sh/resource-policy: keep`: deleting a CRD deletes every
custom resource of that kind, so an accidental uninstall would withdraw every deployment
the cluster serves.

Without the operator the agent writes the file directly, which is a working stamp with
one fewer moving part and no Kubernetes permissions at all. Both paths are verified.

## Install

```bash
docker build -f deploy/images/agent.Dockerfile      -t fabric/agent:0.1.0 .
docker build -f deploy/images/data-plane.Dockerfile -t fabric/data-plane:0.1.0 .

helm install stamp deploy/helm/fabric-stamp \
    --set controlPlane.url=https://control.fabric.example \
    --set controlPlane.jwtIssuer=https://control.fabric.example \
    --set modelHost.url=http://vllm:8000 \
    --set enrollment.token=<single-use token>
```

The chart refuses to render without a control-plane URL, a token issuer, a model
host, and an enrollment token, because each omission produces a pod that cannot
work. The enrollment token Secret is single use and can be deleted once the agent
reports enrolment; the agent will not ask for it again.

Only the inference port is published. The Service does not expose the
administrative port, and the default NetworkPolicy admits ingress to the inference
port alone, so nothing in the cluster can drain usage or read internal state.

The ServiceAccount is created with no Role or RoleBinding and with token mounting
disabled: the agent creates no Kubernetes resources, so it needs no API access at
all.

## Verification

`deploy/scripts/kind-e2e.sh` builds the images, creates a throwaway cluster,
installs the chart against a control plane on the host, and asserts:

- all three containers become ready, which requires the data plane to have fetched
  verification keys without serving any request
- the stamp enrols outbound and serves nothing before a placement exists
- a placement reaches the running pod without a restart
- inference through the Service returns the customer's model alias, and a token the
  control plane did not sign is refused
- the served call's token counts arrive centrally, attributed to that account and
  stamp
- the reported status is readable at `observed_generation=1` with reason
  `AgentAppliedLocalConfiguration`
- a deleted pod recovers on its own, with no second enrollment

Set `FABRIC_DATABASE_URL` to a PostgreSQL URL to exercise the production engine;
SQLite is the default. `FABRIC_KEEP_CLUSTER=1` leaves the cluster for inspection.
