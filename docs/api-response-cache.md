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
as the write. Deployment status reports advance the deployment owner's generation
when availability, generation, replicas, endpoint, or conditions actually change,
so a newly ready replica is visible on the next dashboard refresh. Repeated identical
reports keep the management cache warm while refreshing the recorded report time.
Stamp inventory keys also include the newest durable stamp update timestamp and
stamp count. Heartbeats and GPU metrics therefore refresh inventory immediately
without evicting unrelated deployment or account responses. Accepted
usage records invalidate the customer accounts resolved from the placements,
including when a system-owned managed stamp reports on behalf of a customer.
Rejected and duplicate usage records do not advance these generations. Old keys
expire naturally; Redis deletion is not required for correctness.

Redis reads and writes each have a 200 ms deadline, no connection retries, and a
32-connection limit per API process. After a connection failure the process bypasses
Redis for five seconds and reads PostgreSQL directly. Responses above 1 MiB bypass
the cache. These bounds prevent a stalled cache from stalling the dashboard.

JWT verification reuses the RSA public key loaded at process startup. Every request
still verifies its signature, issuer, audience and expiry; it avoids parsing the
private PEM on each read. A controlled local measurement reduced verification from
38.072 ms to 0.064 ms median for the same token and key. Existing cluster API cache
hits measured approximately 50–63 ms before this change, with about 40 ms spent in
that repeated key parsing. These are component measurements, not a dashboard
navigation latency guarantee.

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

## Navigation and gateway validation on 2026-10-06

The follow-up control-plane release uses digest
`sha256:7fea130c264194bf7559656caa885be6a62411459a42c23bcffe1b963efd28be`
(ACR run `cun`, Helm revision 13). Both API replicas are Ready. Warm authenticated
reads through the cluster service measured median 8.65 ms for deployments, 8.22 ms
for status and 9.27 ms for stamp inventory, compared with the earlier 50–63 ms
cache-hit samples. These are service timings rather than browser page timings.
Sixteen reads added 13 Redis hits and three misses; the account cache generation
stayed at 93 across a 16-second heartbeat interval. JWT verification measured
0.053 ms median inside the updated pod.

The console now reuses visited pages for ten seconds and fully prefetched pages
for Next.js's minimum thirty-second window. Sidebar destinations are prefetched
on hover or keyboard focus. Server-rendered duplicate account reads are memoized
only for the current render, and deployment inventory reads run in parallel.
Live pages still refresh every eight seconds; revisiting older content starts
an immediate refresh while keeping that content visible. Account selection,
authentication and mutations retain their existing invalidation boundaries.

The hosted console's `v1/vercel.json` explicitly approves only
`https://inference.hexelstudio.com` as a stamp-reported inference origin and places
its server function in Mumbai (`bom1`), near the Central India control plane.
The deployed public health response confirmed `bom1::bom1`, replacing
`bom1::iad1`. This file applies to this Vercel installation; Helm installations
continue to configure their gateway or allowed domains through chart values.
Playground availability now refreshes without discarding its prompt or responses.

A live authenticated request returned all four current gateway aliases, including
`qwen` and `qwen3.5-4b`. The `qwen` stream returned HTTP 200, terminal `[DONE]` and
three reported completion tokens in 0.130 seconds for a short prompt. This is a
single warm request, not a throughput benchmark. The authenticated browser session
was unavailable for end-to-end UI verification. Regression tests cover gateway
approval, readiness and generation mismatch; all five CI jobs passed for `c597098`.

The actual console playground modules were also executed against the public control
plane and inference gateway, using short-lived diagnostic credentials held only
in process memory. They returned five choices (Auto plus the four deployment
aliases), no unavailable notice, and a completed HTTP 200 SSE response through
the playground handler in 0.653 seconds. Authentication/session resolution was
substituted for this integration check; the real Auth0 browser flow was not tested.
The handler now resolves its account context once and reuses one control token
across deployment, placement and status reads before obtaining its inference token.

The matching standalone console image is available for Helm installations at
`acrfabricinference.azurecr.io/fabric/console@sha256:dccdc95d6667f0586427f8a19bd725f9d96f2cdb54d49ff3e99d02dcf11be8f8`
(ACR build `cup`). Use the image overlay in
`deploy/releases/console-cache-20261006/control-plane-images.yaml` with your
installation's existing configuration. All five CI jobs passed for `9f4b40c`.
