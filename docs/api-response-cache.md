# Dashboard and management API caching

The control plane uses one shared Redis-compatible cache for authenticated reads.
Both API replicas use the same in-cluster Valkey `ClusterIP` service. This is a
single disposable cache instance; PostgreSQL remains authoritative. A Valkey
restart discards cached responses and the next reads rebuild them.

| Reads | Default TTL |
| --- | --- |
| Deployment list, desired configuration, placements | 60 seconds |
| Account configuration, members, entitlements, identity-provider metadata, API-key metadata, service principals | 60 seconds |
| Deployment status, account/deployment usage, stamp inventory and reported metrics | 3 seconds |

Authentication, account binding, and required scopes are checked before every
cache lookup. Keys include the account UUID, a durable account generation, the
resource, and route parameters. Access tokens, credential secrets, token exchange,
onboarding, enrollment, agent desired-state delivery, health probes, and mutation
responses are not cached.

Mutations advance the account's cache generation in the same database transaction
as the write. Deployment status reports also advance the deployment owner's
generation, so a newly ready replica is visible on the next dashboard refresh.
Heartbeats and GPU metrics invalidate their stamp owner's generation. Accepted
usage records invalidate the customer accounts resolved from the placements,
including when a system-owned managed stamp reports on behalf of a customer.
Rejected and duplicate usage records do not advance these generations. Old keys
expire naturally; Redis deletion is not required for correctness.

Redis reads and writes each have a 200 ms deadline, no connection retries, and a
32-connection limit per API process. After a connection failure the process bypasses
Redis for five seconds and reads PostgreSQL directly. Responses above 1 MiB bypass
the cache. These bounds prevent a stalled cache from stalling the dashboard.

Enable the in-cluster cache with values like these:

```yaml
cache:
  existingSecret: fabric-api-cache
  existingSecretKey: redis-url
  passwordSecretKey: password
  ttlSeconds: 60
  liveTtlSeconds: 3
  timeoutSeconds: 0.2
  maxConnections: 32
  maxValueBytes: 1048576
  internal:
    enabled: true
    maxMemoryMiB: 192
    resources:
      requests: {cpu: 50m, memory: 64Mi}
      limits: {cpu: 250m, memory: 256Mi}
```

Create the existing Kubernetes Secret in the control-plane namespace with two
keys: `password` and `redis-url`. The URL must contain that password and the actual
release service name, for example
`redis://:<URL-encoded-password>@<control-plane-fullname>-cache:6379/0`. Use a
protected local file or a secret manager to create it; keep the credential out of
Git and shell history. Helm passes only Secret references to the workloads.

The cache has no public ingress, no service-account token, no persistent volume,
and an ingress NetworkPolicy permitting only the release's API pods. Its eviction
policy is `allkeys-lru`. Cache configuration changes roll the API pods automatically.

For an existing Redis or highly available managed Redis installation, set
`cache.internal.enabled: false` and point `cache.existingSecret` at its URL instead.
Do not add independent Redis replicas behind the same service: they would return
different caches without replication or a supported failover endpoint.

To check an installation, confirm the cache pod and both API pods are Ready, read
the same dashboard resource repeatedly, and inspect Redis `INFO stats` for growing
`keyspace_hits`. When status or deployment intent changes, the durable generation
advances and the next request loads its new value. PostgreSQL-backed authorization
and status continue working when the cache is temporarily unreachable.

## AKS validation on 2026-10-06

The existing `cp-fabric-control-plane-cache` Valkey service remained Ready while the
control plane upgraded to two Ready replicas. The deployed control-plane digest is
`sha256:933c42eb8787711a1ed1270c8547a26a5f5aebac0644b9f841fc93943bac2c1d`;
its ACR build run is `cum`. The stamp agent and collector use
`sha256:079683653592b7768785094d5fb6f448726f39c305eec828146e62d2f6b80933`.
The existing operator and GPU model-host workloads were retained during this fix.

Repeated authenticated reads of deployment configuration, status, placements and
usage produced four additional Redis cache hits. The observed TTLs were 59 seconds
for configuration and three seconds for live status/usage. Ready-state reports
advanced the account generation from its prior value of 8 to 16 while the agent
replayed existing placements; old cache entries could no longer be current.

The `qwen` deployment loaded `Qwen/Qwen2.5-Coder-3B-Instruct`. Its Kubernetes CR was
ready at generation 1 while the independent control-plane placement expected
14. The agent previously copied the CR counter into the placement report. The fix
preserves each placement generation independently, including after restarts, and
retries failed intent publication before acknowledging a newer generation.

The live API now reports placement desired/observed generation 14, `ready`, one
ready replica, zero unavailable replicas and `https://inference.hexelstudio.com`
as its gateway. Authenticated `/v1/models` returned HTTP 200 and included `qwen`. A real
`/v1/chat/completions` request for that alias returned HTTP 200 in 2.048 seconds
with three completion tokens. The console derives its Ready state from these
matched observations.

Validation included 227 PostgreSQL tests under a non-superuser role without
BYPASSRLS, the full Go suite, four new agent regression tests, three cache chart
checks, and an actual Valkey check of shared hits, three-second TTL and generation
invalidation. Cache downtime is covered by bounded fallback tests.
