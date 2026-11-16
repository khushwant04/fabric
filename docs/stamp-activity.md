# Stamp activity log and removing a stamp

Right after running the [one-command bootstrap](getting-started.md), the owner opens
**Inference stamps → the stamp** in the console and watches its **Activity** log, newest first:

```
Ready for deployments
GPU runtime OK: 1 nvidia.com/gpu allocatable
1 × NVIDIA RTX PRO 6000 Blackwell Server Edition found (96 GiB)
Enrolled with the control plane as rtx-pro-6000
```

Later entries explain everything else that happens to the machine.

## What is recorded

The agent records these events (`agent/internal/activity`, `agent/internal/agent/activity.go`).
Repeated observations are recorded once, and again only when they change.

| Kind | When | Severity |
|---|---|---|
| `enrolled` / `identity_reused` | first enrollment / a restart that reused the stored identity | success / info |
| `gpus_measured` | product, count and memory per GPU class, when they change | info |
| `gpu_runtime` | allocatable `nvidia.com/gpu` present, or missing with a hint (device plugin, RuntimeClass, re-run the bootstrap) | success / warning |
| `ready` | heartbeat accepted and the GPU runtime works | success |
| `credentials_rotated` | a rotation succeeded (requested or by age); failures are `error` | success |
| `desired_state_applied` | a new desired-state generation was published, naming what changed | info |
| `image_pull_started` / `image_pull_finished` | from model-host pod events, with duration and image size | info / success |
| `model_host_failed` | a crash or pull failure, with the same diagnosis the Logs tab shows | error |
| `error` | reconcile or capacity-measurement failures | error |

The control plane adds `stamp_removed` when the stamp is removed.

Events wait in a bounded buffer (200) until an upload succeeds, so a stamp that lost its
connection reports what happened once it is back. Each event carries an id the control plane
deduplicates on. The agent masks anything credential-shaped (`hf_…`, `Bearer …`, Fabric
credentials, `password=`, JWTs, private keys) before buffering, and the control plane masks
again before storing. Turn uploads off with `agent.reportActivity=false`
(`--report-activity=false`).

## Storage and access

```
POST /v1/stamps/{stamp_id}/events                         agent credential, stamp:status:write
GET  /v1/accounts/{account_id}/stamps/{stamp_id}/events   stamps:read
```

The upload is authenticated exactly like the host report: the stamp credential is verified,
must match the path, and needs the `stamp:status:write` scope every agent credential already
holds. The owning account is always the stamp's own, read from the stamp record, never from
the request; unknown fields are rejected. At most 50 events per upload, 16 scalar details per
event; timestamps more than 5 minutes in the future are clamped.

`stamp_events` keeps the newest 200 events per stamp. Rows carry the stamp's account and the
table has the same forced row-level security policy as every other account table
(migration `0007_stamp_events`); `test_stamp_events_are_isolated_by_row_level_security` proves
another account cannot read or write them. The read is not cached, and a removed stamp keeps
its log. The console polls it every 4 s while the stamp page is open and visible.

## Removing a stamp

**Remove** (a row action and a button on the stamp card and page) calls
`DELETE /v1/accounts/{account_id}/stamps/{stamp_id}` after a confirmation that explains the
consequences:

- the agent and telemetry credentials are revoked at once, so the agent stops syncing;
- deployments placed there are **kept**: their placements become `stamp_removed`, and a
  deployment with no other live placement is marked `stamp_removed` too. Its page says
  *Stamp removed* and offers **Redeploy** onto another stamp. Nothing is deleted on the
  owner's behalf, and an edit does not turn the dead placement back into "assigned";
- the stamp leaves the list and the fleet totals; **Show removed (N)** brings it back.

A stamp with no heartbeat for over 5 minutes is listed under a warning with a **Remove**
action, since a machine that was switched off for good otherwise looks merely stale forever.
