# Packaging and Deployment

**Status:** Implemented for a stamp — container images in [`deploy/images/`](../../deploy/images/) and a Helm chart in [`deploy/helm/fabric-stamp/`](../../deploy/helm/fabric-stamp/) install a working inference stamp on Kubernetes. Verified on a real cluster by [`deploy/scripts/kind-e2e.sh`](../../deploy/scripts/kind-e2e.sh).
**Not included:** a CRD or operator (none exists), a chart for the control plane, GPU scheduling, an ingress or TLS termination, and a model host. Fabric does not deploy vLLM.
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
