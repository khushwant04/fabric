# Edge gateway: one inference URL for every stamp

Every stamp has its own gateway and its own public URL. The edge gateway puts one URL in
front of all of them. A caller sends every request to the edge. The edge checks the token,
finds a stamp that serves the requested model for the caller's account, and streams the
request to that stamp's gateway. The stamp gateway checks the token again and serves the
request.

The decision is recorded in [ADR 0021](context/adrs/0021-edge-gateway.md). The edge runs
from the data-plane image as `python -m fabric_data_plane.edge`. It is installed with the
control-plane chart and is disabled by default.

```
                              callers (OpenAI / Anthropic SDKs, curl, agents)
                                                  |
                                   https://inference.example.com
                                                  |
   +----------------------------------------------v-------------------------------------+
   |  AKS (control-plane cluster)                                                       |
   |                                                                                    |
   |   +----------------------+   routing table    +-----------------------------------+|
   |   |    CONTROL PLANE     |  (background poll, |            EDGE GATEWAY           ||
   |   |  deployments,        |   ETag, cached on  |  1. verify inference JWT locally  ||
   |   |  placements, stamps, |   disk)            |  2. read `model` (bounded body)   ||
   |   |  heartbeats, status  |------------------->|  3. candidates = this account's   ||
   |   |  JWKS                |------------------->|     placements of that model      ||
   |   +----------------------+   signing keys     |  4. pick a stamp, stream request  ||
   |              ^                                 +----------------+------------------+|
   +--------------|-------------------------------------------------|-------------------+
                  | heartbeat: inference_url,                       | HTTPS, verified TLS,
                  | region, GPUs; status                            | caller's own token
                  | (outbound from each stamp)                      |
      +-----------+-------------+----------------------+------------+--------------+
      |                         |                      |                           |
+-----v--------------+  +-------v------------+  +------v-------------+  +----------v---------+
| stamp: AKS T4 pool |  | stamp: AKS A10 pool|  | stamp: k3s on GCP  |  | stamp: k3s on a VM |
| gateway (verifies  |  | gateway            |  | gateway            |  | gateway            |
|  again, limits,    |  | model hosts        |  | model hosts        |  | model hosts        |
|  meters, auto)     |  |                    |  |                    |  |                    |
| model hosts        |  |                    |  |                    |  |                    |
+--------------------+  +--------------------+  +--------------------+  +--------------------+
```

Inference never passes through the control plane. The edge reads the routing table and
signing keys in the background and serves from memory. After a restart it serves from
the copy saved on disk. Stopping the control plane stops the edge from learning about
changes. It does not stop the edge from serving.

## How stamps from AKS, GCP and other VMs appear behind one URL

To the edge, a stamp is a place that serves some of an account's deployments at an HTTPS
URL. The cloud and the Kubernetes distribution do not matter. A stamp is routable when
all of the following are true:

1. Its gateway is exposed over HTTPS with a certificate the edge trusts: a public CA, or
   the CA in `edge.stampCaBundle`. [Native stamp exposure](stamp-native-endpoint.md)
   sets this up with an Ingress and cert-manager.
2. Its agent reports that address as `inference_url` (`--inference-url` /
   `FABRIC_AGENT_INFERENCE_URL`, or the stamp chart's exposure settings). The control
   plane accepts only an HTTPS base URL without credentials.
3. It is not revoked, and it has heartbeated within `FABRIC_EDGE_STAMP_LIVENESS_SECONDS`
   (control plane setting, default 300 s, the same window placement uses).
4. A deployment is placed on it. Placements the stamp has reported ready are preferred.
   Placements not yet ready are kept as a fallback.

Examples: an AKS stamp with a T4 node pool, a second AKS stamp with an A10 pool, a
single-node k3s stamp on a GCP VM with an RTX PRO 6000, and a k3s stamp on any other VM.
Each one is enrolled, reports its own `inference_url` and region, and appears in the edge's
routing table. Callers see one URL, and `GET /v1/models` on the edge lists every model the
account has across all of them.

## What the edge does on each request

| Step | Behaviour |
|---|---|
| Authenticate | Verifies the Fabric inference JWT (audience, issuer, signature, expiry, `inference:invoke`) with the data plane's own `verify_inference_token`. A bad token gets 401 before any stamp is contacted. `/v1/messages` also accepts `x-api-key`. |
| Read the model | Reads the body up to the stamp's limits (16 MiB JSON, nesting depth 128; audio up to 25 MiB plus form overhead). For audio, `model` comes from the multipart form. |
| Find candidates | Looks only at the caller's account: a deployment with that alias or that deployment id. `auto` and `cascade` (or no model) make every stamp holding one of the account's deployments a candidate, and the stamp gateway chooses the deployment. Another account's model is never a candidate, so naming it gets 404. |
| Order candidates | Stamps not ejected first, then placements reported ready, then the preferred region (`X-Fabric-Region`, or `FABRIC_EDGE_DEFAULT_REGION`), then the fewest requests this edge has in flight to the stamp. Ties are broken at random. |
| Affinity | `X-Fabric-Run-Id` sends a request to one stamp every time. The stamp is chosen by rendezvous hashing on the account and run id. This is strict: if that stamp is down, the request fails, because the run's budget and trace exist only there. A session key (`X-Fabric-Session`, `X-Session-Id`, `X-Conversation-Id`, or the body's `user`) is also sticky so the stamp's prefix cache stays warm, but it fails over. |
| Forward | Sends the original bytes. The caller's `Authorization` / `x-api-key` header is forwarded unchanged. Hop-by-hop headers are removed and `X-Forwarded-*` headers are added. The response is streamed back chunk by chunk with nothing buffered, and SSE passes through as-is. The response carries `X-Fabric-Edge-Stamp: <stamp name>`. |
| Retry | Moves to another stamp only after a connection failure (connect error or connect timeout). Once a stamp may have received the request, the edge never replays it, because the request could run and be billed twice. This is the same rule the stamp gateway applies to model hosts (`_safe_to_retry_transport_failure`). A stamp's own error responses, such as 429 or 503, are passed through unchanged. |
| Passive health | After `stampFailureThreshold` consecutive connection failures, a stamp is ejected for `stampRecoverySeconds`. Ejected stamps are tried last rather than dropped. |

Routes: `/v1/chat/completions`, `/v1/completions`, `/v1/responses`, `/v1/messages`,
`/v1/messages/count_tokens`, `/v1/audio/{transcriptions,translations}`, `/v1/agent/runs`
(routed like chat, with run-id affinity when the header is sent), `GET /v1/models`
(answered from the table, no stamp contacted), `GET /v1/runs` (collected from each of the
account's stamps), and `GET`/`DELETE /v1/runs/{id}` (asks the account's stamps in affinity
order until one has the run).

## What is stamp-local

The edge keeps no request state that matters across requests. All of the following lives
on one stamp:

- **Agent runs, budgets and traces** are held in that stamp gateway's memory. This is why
  run ids have strict affinity. A run never moves to another stamp. If its stamp is lost,
  the run is lost too.
- **Prefix caches** live on that stamp's model hosts. Session keys keep a conversation on
  one stamp.
- **`model: "auto"` and `model: "cascade"`** choose among the deployments on the stamp the
  edge picked. A cascade escalates only across tiers present on that stamp. To give every
  request the full cascade, place every tier on every stamp, or on the same stamps.
- **Limits and metering.** Rate, concurrency and token limits are enforced by each stamp,
  and usage is metered by each stamp and forwarded by its collector. **The edge meters
  nothing and enforces no limits**, so usage is never counted twice. Limits are therefore
  per stamp: an account served by two stamps can use up to two stamps' allowance.

## Routing table and its credential

The control plane serves `GET /v1/edge/routing-table`. For every account, the table lists
the deployments that have at least one placement on a live stamp that reports an
`inference_url`. For each placement it gives the stamp id, name, region, gateway URL,
readiness, a GPU summary and the heartbeat age. It also includes `revision` (a
non-decreasing high-water mark) and an `ETag`. The table contains no credentials,
deployment specs, in-cluster endpoints or usage.

Only one credential can read it:

```bash
# On a host with the control plane's database credentials (for example the migration image):
python -m app.cli create-edge-credential --name fabric-edge
#   account   <system account id>
#   key_id    <id>  scopes=edge:routing:read
#   api_key   fab_key_...          <- shown once
kubectl create secret generic fabric-edge-credential --from-literal=api-key='fab_key_...'
```

Authorization works in layers:

- The scope `edge:routing:read` is outside the delegable scope set. The API-key endpoints
  reject it, so no account can mint it. Only the CLI command above, which needs database
  access, creates a key with it. That key holds this scope and nothing else: it cannot
  invoke inference or change anything.
- Token exchange carries the scope into a control token only for a service principal of
  the Fabric system account. The same scope copied onto any other key is dropped.
- The endpoint checks the scope again, and checks that the caller is a service principal
  of the system account.
- The cross-account query runs inside `tenancy.elevated`. This is the explicit, scoped
  elevation that placement and managed-capacity grants already use, and it ends when the
  read ends. Row-level security is unchanged for every other path. The PostgreSQL suite
  tests this (`test_the_edge_routing_table_spans_accounts_only_through_its_elevation`).
- Each full read (200) is audited on the system account as `edge.routing_table.read`. A
  conditional read that returns 304 is not audited, so a polling edge does not flood the
  audit trail.

Revoke the credential like any other system-account API key. The edge then keeps serving
its last table until a new credential is installed.

## Failure modes

| Failure | What happens |
|---|---|
| Control plane or database down | The edge keeps serving the table and keys it holds. `fabric_edge_routing_table_age_seconds` grows, and `/admin/routing` shows `last_poll_error`. Placements made during the outage are not routable through the edge until it recovers. Callers can still reach those stamps directly. |
| Edge restarted during an outage | Loads the last good table from `FABRIC_EDGE_TABLE_CACHE_PATH` and the keys from `FABRIC_EDGE_JWKS_FILE` if set, then becomes ready. Without a saved table or keys it stays unready, so no traffic is sent to an edge that would refuse everything. |
| A stamp gateway down | Connection failures move requests to another stamp holding the model, and the stamp is ejected. A model placed only on that stamp gets 503 `no_stamp_reachable`. Runs pinned to it get 503. |
| A stamp fails mid-request | The edge returns 503 `stamp_unavailable` before any response bytes are sent, or ends the stream after they are sent. It does not retry, so the request cannot run twice. |
| A stamp stops heartbeating | After the liveness window, its placements leave the table on the next poll. |
| Signing key rotated | A token with an unknown key id triggers a key fetch. These fetches are limited to one per `jwks_min_refetch_seconds`, so made-up key ids cannot flood the control plane. Keys are also refreshed every `jwks_refresh_seconds`. |
| A lagging control-plane replica | A table with a lower revision than the one held is refused. If the control plane stays behind for longer than `revision_regression_grace_seconds`, its table is accepted, which covers a restored database. |
| Token revoked | Same as at stamps: tokens are short-lived, and the stamp checks the token again. |

## Operating it

Helm (`deploy/helm/fabric-control-plane`):

```yaml
edge:
  enabled: true
  credential:
    existingSecret: fabric-edge-credential
  defaultRegion: eastus
  ingress:
    enabled: true
    className: nginx
    host: inference.example.com
    annotations:
      nginx.ingress.kubernetes.io/proxy-buffering: "off"
      nginx.ingress.kubernetes.io/proxy-read-timeout: "600"
      nginx.ingress.kubernetes.io/proxy-body-size: "32m"
    tls:
      secretName: inference-example-com-tls
  persistence:
    enabled: false   # emptyDir: survives container restarts, not rescheduling
```

When the console is enabled, the chart also passes the edge's HTTPS URL to it as
`FABRIC_EDGE_INFERENCE_URL` (override with `console.edgeInferenceUrl`), and as
`FABRIC_INFERENCE_URL` when `console.inferenceUrl` is empty. With the edge configured, the
console's playground and agent playground send every deployment, and `model: "auto"` and
`model: "cascade"`, through the edge, and Overview and Stamps show an **Edge gateway** card
with the URL. Without it, the console sends each deployment to the gateway of a stamp holding
a ready placement (an approved status endpoint or stamp `inference_url`), falling back to
`FABRIC_INFERENCE_URL`.

Listeners:

- **Public (8080):** inference routes, plus `/healthz` and `/readyz`.
- **Admin (8081):** `/metrics`, `/healthz`, `/readyz`, and `/admin/routing`. No Service
  publishes this port. The routing summary includes stamp names, regions, gateway
  origins, placement counts and health. It includes no account ids, model names or
  credentials.

Metrics: `fabric_edge_requests_total{stamp,outcome}`,
`fabric_edge_request_duration_seconds{stamp}`, `fabric_edge_stamp_ejections_total`,
`fabric_edge_retries_total`, `fabric_edge_stamp_in_flight`, `fabric_edge_stamp_available`,
`fabric_edge_routing_table_age_seconds`, `fabric_edge_routing_table_revision`,
`fabric_edge_routing_table_from_disk`, `fabric_edge_routing_table_fetches_total{outcome}`,
and `fabric_edge_verification_keys`.

The edge never logs tokens or request bodies. Uvicorn's access log records only the
method, path, status and client address.

Settings use the `FABRIC_EDGE_*` prefix. See `data-plane/fabric_data_plane/edge/config.py`
for the full list. The main ones are `CONTROL_PLANE_URL`, `API_KEY_FILE`, `JWT_ISSUER`,
`JWKS_URL`, `JWKS_FILE`, `TABLE_CACHE_PATH`, `STAMP_CA_BUNDLE`, `STAMP_ADDRESS_OVERRIDES`,
`DEFAULT_REGION`, `POLL_INTERVAL_SECONDS`, `READ_TIMEOUT_SECONDS`, `MAX_ATTEMPTS`,
`STAMP_FAILURE_THRESHOLD` and `STAMP_RECOVERY_SECONDS`.

## Try it locally

```bash
make demo-edge
```

This starts a control plane, two stamps (Go agents plus HTTPS gateways with stub model
hosts, `stamp-a` in us-east and `stamp-b` in eu-west) and the edge. It then shows the
following on one URL:

- `model-a` is served by stamp-a and `model-b` by stamp-b.
- `model-shared` fails over to stamp-b when stamp-a's gateway stops.
- Serving continues after the control plane stops, including after an edge restart from
  its cached table.
