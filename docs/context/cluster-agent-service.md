# Cluster Agent and Usage Collector

**Status:** Implemented (initial version) — [`agent/`](../../agent/) ships two Go binaries: `fabric-agent` enrolls a stamp, pulls desired state, renders the data plane's local configuration, and reports status; `fabric-collector` drains the data plane's usage buffer and forwards it to the control plane.
**Design reference:** [ADR 0007](adrs/0007-cluster-agent-and-central-telemetry.md), [ADR 0008](adrs/0008-account-scoped-tenancy-and-stamp-credentials.md), [Operator and Deployment](operator-deployment.md).
**Not included yet:** runtime and GPU metrics collection. With the operator enabled the agent declares `FabricModelDeployment` resources and forwards the operator's verdict; without it, status reports what the agent itself applied.

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
| `telemetry-credential` | 0600 | The write-only telemetry credential, written for the collector's own Secret |

The agent credential is used for every authenticated call. The agent never presents
the telemetry credential, which a test asserts. It hands that credential to the
collector through a separate file rather than sharing `credentials.json`, because
that file also holds the agent credential and a collector able to read it could pull
desired state and write status. The hand-off is optional, so a stamp running no
collector still enrolls. `deployments.json` is written atomically, because the data plane may
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

## Usage collector

`fabric-collector` is a separate process from the agent, so the two credentials live
in separate Secrets. The agent writes the write-only telemetry credential to its own
0600 file and the collector reads only that; it never opens `credentials.json` and so
cannot obtain the agent credential. The telemetry credential cannot read desired
state, write status, or invoke inference, which the end-to-end run confirms by
attempting a desired-state read with it and getting 401.

| Behaviour | Why it is shaped that way |
|---|---|
| Drained records held in a bounded pending queue | Draining is destructive, so a failed forward would lose them outright |
| Oldest dropped when the queue overflows, and counted | An outage must not grow memory without limit, and loss must be visible |
| A retryable failure keeps records and returns cleanly | The next pass forwards them; the data plane no longer holds them |
| A permanent rejection stops the collector | A revoked credential or stamp never recovers, so the backlog is unforwardable |
| Per-record rejections are logged and discarded | An unplaced deployment or an out-of-window timestamp cannot become valid |
| Duplicates are not an error | Delivery is at-least-once by design |
| Backlogs are chunked to the server's 500-record limit | A larger backlog would otherwise be refused wholesale |
| An empty buffer produces no control-plane request | Nothing to report is not an event |

The deduplication key is the identifier the data plane assigns when it records the
call, not something the collector invents, so a retried forward is recognised
centrally instead of double counted.

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

# Forward usage once, then exit. The credential is read from a file or the
# environment, never a flag, so it never appears in a process listing.
go run ./cmd/fabric-collector \
    --control-plane https://control.fabric.example \
    --data-plane-admin http://127.0.0.1:8081 \
    --credential-file /var/lib/fabric-agent/telemetry-credential \
    --once

# Continuous forwarding.
go run ./cmd/fabric-collector --control-plane ... --interval 60s
```

Flags also read `FABRIC_AGENT_CONTROL_PLANE`, `FABRIC_AGENT_STATE_DIR`,
`FABRIC_AGENT_STAMP_NAME`, `FABRIC_AGENT_UPSTREAM`, `FABRIC_AGENT_ORCHESTRATOR`,
and `FABRIC_AGENT_REGION`. Pass `--telemetry-credential-file` (or
`FABRIC_AGENT_TELEMETRY_CREDENTIAL_FILE`) to enable the collector hand-off.

The collector reads `FABRIC_COLLECTOR_CONTROL_PLANE`,
`FABRIC_COLLECTOR_DATA_PLANE_ADMIN`, `FABRIC_COLLECTOR_CREDENTIAL_FILE`, and
`FABRIC_COLLECTOR_TELEMETRY_CREDENTIAL`.

## End-to-end verification

Unit tests cover each component against stubs of the others, which cannot show
that the real pieces agree. [`agent/scripts/e2e.sh`](../../agent/scripts/e2e.sh)
runs four real processes: a control plane over HTTP, the compiled agent against it,
the Python data plane serving both listeners from the file the agent produced, and
the compiled collector draining that data plane:

```text
control plane   enroll a stamp, place a deployment on it
     |
     v
agent           enrol once, pull desired state, render deployments.json,
     |          report status back, hand the collector its credential
     v
data plane      load that file, authorize an inference token for the owning
     |          account, refuse a token the control plane did not sign,
     |          buffer the usage of the call it served
     v
collector       drain that buffer and forward it to the control plane, where
                the account can read its own usage
```

Verified by that run:

- enrollment consumes a single-use token
- no placement yields an empty configuration rather than an invented one
- a placement appears with the owning account, upstream, and release
- the reported status is readable through the control API at `observed_generation=1`
- the data plane lists exactly that model for the account and serves it
- an untrusted token is refused
- the telemetry credential is written at 0600 and is refused when it tries to read
  desired state
- the served call's token counts arrive centrally, attributed to that account and
  stamp
- a second collector pass adds nothing, so retries do not double count
