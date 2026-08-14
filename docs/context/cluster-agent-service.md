# Cluster Agent

**Status:** Implemented (initial version) — the Go agent in [`agent/`](../../agent/) enrolls a stamp, pulls desired state, renders the data plane's local configuration, and reports status.
**Design reference:** [ADR 0007](adrs/0007-cluster-agent-and-central-telemetry.md), [ADR 0008](adrs/0008-account-scoped-tenancy-and-stamp-credentials.md), [Operator and Deployment](operator-deployment.md).
**Not included yet:** creating Kubernetes resources, telemetry export, and the Helm bundle. There is no operator, so status reports what the agent itself applied.

## What runs today

| Capability | State |
|---|---|
| One-time enrollment, credentials persisted 0600 | Implemented |
| Restart reuses credentials and never re-enrolls | Implemented |
| Heartbeat with bounded capability report | Implemented |
| Desired-state pull by monotonic generation | Implemented |
| Renders `deployments.json` for the data plane | Implemented |
| Per-assignment owning account, correct for managed stamps | Implemented |
| Withdraws deleted assignments | Implemented |
| Status writes with observed generation | Implemented |
| Stops on a permanent rejection, retries transient ones | Implemented |
| Creating Kubernetes CRs, telemetry export, Helm bundle | Not implemented |

## Direction of travel

Everything is outbound. The control plane never connects to the cluster, so a
stamp behind NAT or a restrictive firewall needs no inbound path:

```text
agent  --enroll------------->  control plane   (once, single-use token)
agent  --heartbeat---------->  control plane
agent  --desired-state------>  control plane   (poll, after_generation=N)
agent  --status------------->  control plane
agent  --write-------------->  deployments.json  (read by the data plane)
```

## State on disk

| File | Mode | Contents |
|---|---|---|
| `credentials.json` | 0600 | Stamp id, stamp account, agent credential, telemetry credential, acknowledged generation |
| `deployments.json` | 0644 | Assignments and the account owning each; no secret |

The agent credential is used for every authenticated call. The telemetry
credential is persisted for the collector and the agent never presents it, which a
test asserts. `deployments.json` is written atomically, because the data plane may
load it at any moment, and entries are sorted so an unchanged assignment set
produces an identical file.

Losing `credentials.json` forces re-enrollment, and enrollment tokens are single
use, so a stamp that loses it needs a new token from an operator. The agent
therefore treats a failure to persist freshly issued credentials as fatal rather
than continuing with an identity it cannot recover.

## Ownership of an assignment

A managed stamp is owned by the Fabric system account while the deployments it
serves belong to different customer accounts. The agent cannot infer ownership
from its own credential, so each assignment carries `account_id`, taken from the
placement. This was added to the desired-state contract while building the agent:
without it, an agent on a managed stamp could only guess, and the data plane's
per-account authorization would have been wrong for every managed placement.

## Ordering guarantees

- Configuration is written **before** the acknowledged generation advances, so a
  crash mid-pass replays the assignment rather than losing it.
- A failed status write does not discard configuration already applied; the pass
  logs and continues.
- A permanent rejection stops the loop. A revoked credential, a revoked stamp, or a
  spent enrollment token never becomes valid again, so retrying would only add
  load. Transient failures and 5xx responses are retried on the next tick.

## Commands

```bash
cd agent
go build ./...
go vet ./...
go test ./...

# One reconcile pass, then exit. The token is read from the environment so it
# never appears in a process listing.
FABRIC_AGENT_ENROLLMENT_TOKEN=<one-time token> \
go run ./cmd/fabric-agent \
    --control-plane https://control.fabric.example \
    --state-dir /var/lib/fabric-agent \
    --stamp-name my-stamp \
    --upstream http://127.0.0.1:8000 \
    --orchestrator k3s \
    --once

# Continuous reconciliation.
go run ./cmd/fabric-agent --control-plane ... --upstream ... --poll 15s
```

Flags also read `FABRIC_AGENT_CONTROL_PLANE`, `FABRIC_AGENT_STATE_DIR`,
`FABRIC_AGENT_STAMP_NAME`, `FABRIC_AGENT_UPSTREAM`, `FABRIC_AGENT_ORCHESTRATOR`,
and `FABRIC_AGENT_REGION`.

## End-to-end verification

Unit tests cover each component against stubs of the others, which cannot show
that the real pieces agree. [`agent/scripts/e2e.sh`](../../agent/scripts/e2e.sh)
runs a real control plane over HTTP, the compiled agent against it, and the Python
data plane against the file the agent produced:

```text
control plane   enroll a stamp, place a deployment on it
     |
     v
agent           enrol once, pull desired state, render deployments.json,
     |          report status back
     v
data plane      load that file, authorize an inference token for the owning
                account, refuse a token the control plane did not sign
```

Verified by that run: enrollment consumes a single-use token; no placement yields
an empty configuration rather than an invented one; a placement appears with the
owning account, upstream, and release; the reported status is readable through the
control API at `observed_generation=1`; the data plane lists exactly that model for
the account and serves it; a forged token is refused as `unknown_signing_key`.
