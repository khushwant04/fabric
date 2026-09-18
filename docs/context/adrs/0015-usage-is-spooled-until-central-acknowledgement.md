# ADR 0015: Usage stays in a durable local spool until central resolution is acknowledged

**Decision status:** Accepted  
**Implementation status:** Implemented  
**Date:** 2026-09-18

## Context

ADR 0014 fixed the numbers recorded for streamed requests, but not the durability of those
numbers before central ingestion. The path had two volatile queues and a destructive handoff:

1. the data plane appended a `UsageRecord` to an in-memory bounded deque;
2. `POST /admin/usage/drain` copied and cleared that deque;
3. the collector held drained records in another in-memory bounded slice while forwarding;
4. only a successful `POST /v1/telemetry/usage` made the record durable in PostgreSQL.

A data-plane restart before step 2 lost its deque. A collector restart after step 2 but before
step 4 lost its slice. The record already had the right stable identity for at-least-once delivery,
but neither local component preserved it long enough to use that property.

The trust split must remain unchanged. The data plane derives ownership from the verified
inference principal and local placement but has no telemetry credential. The collector has the
write-only telemetry credential but does not decide account ownership or invoke inference.
Putting the credential in the data plane merely to simplify export would collapse that boundary.

## Decision

### 1. The data plane records into a bounded SQLite spool

`UsageBuffer` keeps its public role but stores records and persistent `recorded`/`dropped` counters
in SQLite. Production sets `FABRIC_DP_USAGE_SPOOL_PATH` to a file on a dedicated PVC. Local tests
and development may omit the path and use an in-memory SQLite database with identical semantics.

A record is committed before request cleanup returns. SQLite runs with WAL journalling and
`PRAGMA synchronous=FULL`; the database file is mode `0600` inside a private directory. The stable
`record_id` is assigned before insertion and survives every restart and replay.

The configured record count remains the bound for newly admitted and unleased records. On startup,
a lowered capacity removes oldest unleased rows immediately. An already-issued lease is protected
even when it is larger than a newly lowered capacity; the spool may temporarily report above the
new bound until that lease resolves. When full, the oldest **unleased** record is removed and
`dropped` increments. An outstanding lease is never evicted because the control plane may already
be processing it. If every retained record is leased, the new record is dropped instead. `recorded` counts every record presented to the spool, including an incoming record that
could not displace a protected lease.

### 2. Reading creates one stable, non-destructive lease

The localhost route keeps its historical name, `POST /admin/usage/drain`, for rolling compatibility,
but no longer drains. It returns:

```json
{
  "lease_id": "uuid-or-null",
  "records": [],
  "count": 0
}
```

At most 500 oldest available records receive one `lease_id`. While that lease exists, every read
returns the same lease regardless of the requested batch size. There is exactly one outstanding
lease: the stamp has one collector, and serialising its handoff removes expiry, ownership and
concurrent-ack races.

`POST /admin/usage/ack` accepts the lease ID **and expected record count**. Inside the same
transaction, the spool compares that precondition with the rows carrying the lease ID and deletes
only on an exact match. This prevents a malformed/truncated lease response from acknowledging
records the collector never received. The last resolved lease/count is retained as a one-row
idempotency marker. A repeated matching acknowledgement reports the original **logical**
`acknowledged` count, `deleted: 0`, and `already_acknowledged: true`, so the collector can accept a
lost-response retry without weakening the exact-count check.

An old collector ignores the extra `lease_id`, forwards the records, and then sees them again. That
causes central duplicates, not double-counting or local deletion. In the opposite rollout order, a
new collector first reads `/admin/usage` and requires `export_mode=lease_ack` before it ever POSTs
to the historically destructive route. Against an old data plane it waits without draining; once
the data-plane image upgrades, normal leasing begins.

### 3. The collector holds no usage backlog

The collector leases, forwards, and acknowledges. It acknowledges only after the control plane
has resolved every record in the batch as one of:

- accepted;
- duplicate (the expected result after a lost acknowledgement);
- permanently rejected at record level.

A retryable transport/API failure leaves the lease untouched. A permanent credential or stamp
failure stops the collector and also leaves it untouched, so credential repair can resume later.
If central ingestion succeeds but local acknowledgement fails, the next pass replays the same IDs;
central deduplication converts them to duplicates and the next acknowledgement deletes them.

The collector validates the local lease envelope before forwarding: `count` must equal the record
array, empty/nonempty lease identity must match, lease and record IDs must be valid and unique, and
the batch cannot exceed 500. It then validates central protocol completeness before acknowledging: counts must be
non-negative and `accepted + duplicates + rejected` must equal the lease size; every rejection
must have one unique in-range index and a reason. An empty, partial, or inconsistent 2xx response
is retryable and leaves the lease.

The old `queue-capacity` flag now bounds records from **new leases** resolved by one pass, not
volatile memory. Each lease is still capped at the central API's 500-record contract. An existing
stable lease may exceed a subsequently lowered pass budget and is resolved whole rather than split
or abandoned. Backlog size and drops belong to the data-plane spool's administrative state.
Central accepted/duplicate/rejected counts are logged even if local acknowledgement fails, with a
separate acknowledged count so the replay remains operationally honest.

### 4. Usage gets a separate retained volume

The spool must not share the agent-state claim. All containers run under the same pod UID, so
mounting agent state into the data plane would expose the long-lived agent credential and defeat
the credential split above.

The stamp chart creates a dedicated `usage-spool` PVC by default, mounts it only in the data plane
at `/var/lib/fabric-usage`, and sets the SQLite file inside a data-plane-created `0700` private
subdirectory. The database, WAL and shared-memory sidecars are continuously enforced as `0600`.
The PVC has
`helm.sh/resource-policy: keep`: uninstalling a chart is not evidence that unacknowledged usage may
be erased. Operators delete it explicitly after confirming the spool is empty. An existing claim
may be supplied. Disabling persistence uses `emptyDir` and emits a warning; that mode is for
disposable development stamps only.

## Consequences

### Positive

- Data-plane and collector restarts do not lose queued or in-flight usage.
- A crash at any point produces either the original delivery or an idempotent replay.
- The inference process still has no telemetry credential, and the collector still cannot choose
  account ownership.
- One durable queue replaces two independently bounded volatile queues.
- Backlog, available, leased, recorded and dropped counts are observable on `/admin/usage`.

### Negative

- Completing a request now includes one local durable SQLite transaction. This is a small disk
  write on the completion path, deliberately chosen over silently losing billable events. The
  first runtime write fault can be discovered only after that request completed model work; all
  serving leases are still released, readiness becomes false, and every later request is rejected
  before reaching the model until the data-plane process restarts against healthy storage.
- The stamp needs another PVC and operators must manage its retention/deletion.
- SQLite is a single-process spool, matching the current one-data-plane-process stamp. Horizontal
  data-plane replicas require separate per-replica spools or a shared durable queue; this ADR does
  not pretend one RWO SQLite file is that shared queue.
- Count-bounded retention does not guarantee seven-day delivery. During a sufficiently long or
  high-volume outage, oldest unleased records are deliberately dropped and counted; records older
  than the control plane's seven-day acceptance window are permanently rejected when replayed.

### Neutral

- Central ingestion, ownership resolution, the stamp-namespaced dedup key and PostgreSQL schema do
  not change.
- The route remains localhost-only and unauthenticated by application code, as before; pod network
  isolation is its security boundary.
- Direct `UsageBuffer.drain()` remains as a compatibility/testing helper but is not exposed by the
  production admin protocol.

## Alternatives considered

- **Persist the collector's pending slice instead.** Rejected: records can still disappear before
  the collector drains them, and it requires a writable credential-bearing collector volume.
- **Let the data plane push directly to the control plane.** Rejected: it puts the telemetry
  credential on the customer request path and couples inference availability to central export.
- **Delete after central ingestion without a lease ID.** Rejected: a collector restart cannot know
  which local records correspond to an earlier response, and deleting by record IDs enlarges and
  complicates both sides of the protocol.
- **Lease expiry.** Rejected for one collector. Expiry permits two collectors to process one batch
  concurrently and adds clock/renewal failure modes without improving recovery; a stable lease is
  naturally resumed after restart.
- **One JSON file rewritten per record.** Rejected: repeated full-file rewrites grow with backlog,
  and correct crash durability would reproduce journalling poorly. SQLite already supplies atomic
  transactions, WAL recovery and bounded indexed selection.
- **Reuse the agent-state PVC.** Rejected: same-UID containers could read the agent credential.
- **Drop leased records when full.** Rejected: they may already be committed centrally; preserving
  their stable handoff is more important than admitting a newer record.

## Verification

Tests prove: file-backed records and counters survive reopen and a subprocess hard exit with a live
WAL; all live SQLite files and their private directory have restrictive modes; an outstanding lease
survives reopen with the same ID and records; acknowledgements are idempotent; duplicate local
record IDs are idempotent; lowering capacity trims oldest unleased records but preserves an
oversized existing lease; overflow never evicts a lease; runtime storage failure releases all
streaming resources, fails readiness, and gates later inference before model work; the admin route
replays until ack and empties only after ack; ownership and record identity remain unchanged;
collector restart needs no local pending state; a new collector never calls an old destructive
drain; transient and permanent API failures leave the lease; incomplete/inconsistent 2xx responses
leave it too; accepted, duplicate and per-record rejection responses ack; a lost ack response logs
the central result, replays as a duplicate, then deletes; an existing lease may exceed a lowered
pass budget; more than 500 records use multiple acknowledged leases; the executable Helm
rendering test covers a generated retained claim, an externally supplied claim, the private SQLite
path/mount, and explicit ephemeral mode. The complete data-plane, agent, control-plane
interoperability, PostgreSQL and Helm suites remain required before merge.
