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
