# Inference Data Plane

**Status:** Implemented (initial version) — the ingress in [`data-plane/`](../../data-plane/) verifies Fabric inference tokens locally and proxies OpenAI-compatible requests.
**Design reference:** [Security and Identity](security-identity.md), [Architecture Requirements](architecture-requirements.md) AR-DP01/02/03 and AR-ID03/04/05.
Configuration is re-read while running. The agent rewrites `deployments.json`
whenever placement changes, so a registry read once at startup would serve a snapshot
from boot: a new placement would return `model_not_found` until the pod restarted and
a withdrawn one would keep being served. The file is re-read only when its
modification time or size changes. A read that fails keeps the last good set rather
than emptying it, and a missing file is treated as a fault rather than as "nothing is
placed here", which the agent expresses by writing an empty list.

Both listeners run in one process (`fabric_data_plane.serve`). The usage buffer is in
memory, so an administrative listener in another process would drain a buffer that
never saw a request and report no usage at all. Health probes exist on both
listeners: the administrative one binds to localhost, so a Kubernetes kubelet
probing the pod address can only reach the public one. Readiness requires verification
keys and answers 503 without them, because a probe reads the status code and a data
plane with no keys rejects every request. Keys are fetched at startup and retried, so
readiness does not wait for traffic that will not arrive.

Usage is buffered locally and taken by a collector through
`POST /admin/usage/drain` on the administrative listener. The data plane never pushes
usage and never holds the telemetry credential, which keeps the inference path free of
any export credential. Each record is given a stable identifier at record time so a
retried forward is deduplicated centrally.

Per-account limits are enforced before a request reaches the model host. A rate limit
bounds how often an account may ask, using a token bucket so a burst allowance is
expressible and the limit holds across window boundaries rather than allowing double the
rate at the seam. A concurrency cap bounds how many of its requests are in flight, which
is the limit that actually protects the GPU, since a model server's cost is set by how
many sequences it decodes at once. Both are per account, because the scarce resource is
the device. A refused request answers 429 with `Retry-After` and never reaches the host.

Both default to disabled and are declared in the chart: the data plane cannot know a
host's capacity, and a guess would either waste the GPU or pretend to protect it. State
is per process, so with replicas each pod holds a fraction of the limit; that is recorded
rather than hidden, and the model host behind them remains the real bound.

**Not included yet:** token-based quotas, which need entitlements from the control plane,
and mTLS or network policy to the model host.

## What runs today

| Capability | State |
|---|---|
| Local inference-JWT verification against cached JWKS | Implemented |
| Audience split enforced (`fabric-inference` only) | Implemented |
| `inference:invoke` scope required | Implemented |
| Account ownership of deployments from local configuration | Implemented |
| Serving continues during a control-plane outage | Implemented |
| Key cache seeded from a local file | Implemented |
| Client ownership headers stripped before proxying | Implemented |
| OpenAI-compatible chat and text completions, streaming | Implemented |
| Customer-facing model alias in replies | Implemented |
| Separate administrative listener | Implemented |
| Local bounded usage buffer | Implemented |
| Telemetry export, quotas, rate limiting, mTLS to the host | Not implemented |

## Authorization model

Two independent facts decide a request, and neither comes from the caller:

```text
verified token  ->  account_id
local config    ->  deployment -> account_id, upstream

serve only when the two accounts match
```

The `model` field selects a candidate deployment, which is ordinary
OpenAI-compatible behaviour and carries no authority. Naming a deployment owned by
another account returns `model_not_available` and never reaches a model host.

## Configuration

Copy [`data-plane/.env.example`](../../data-plane/.env.example) to `.env`:

| Variable | Purpose |
|---|---|
| `FABRIC_DP_JWT_ISSUER` | Must match the control plane's `FABRIC_JWT_ISSUER` exactly |
| `FABRIC_DP_JWKS_URL` | Where signing keys are published |
| `FABRIC_DP_JWKS_REFRESH_SECONDS` | Cache lifetime; a stale cache still serves |
| `FABRIC_DP_JWKS_FILE` | Optional public key set on disk for a restart during an outage |
| `FABRIC_DP_DEPLOYMENTS_FILE` | Local deployment configuration |
| `FABRIC_DP_LEEWAY_SECONDS` | Clock skew allowance on time claims |
| `FABRIC_DP_UPSTREAM_TIMEOUT_SECONDS` | Model host timeout, bounding long generations |
| `FABRIC_DP_USAGE_BUFFER_SIZE` | Bounded local usage buffer |

Deployments are described by [`data-plane/deployments.example.json`](../../data-plane/deployments.example.json).
A populated file names real accounts and model releases and is gitignored.

## Local commands

```bash
cd data-plane
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'

cp .env.example .env                          # set issuer and JWKS URL
cp deployments.example.json deployments.json  # set accounts and upstreams

# Inference listener: the only one a customer network should reach.
.venv/bin/python -m uvicorn fabric_data_plane.main:inference --host 0.0.0.0 --port 8080

# Administrative listener, separate process and port (AR-DP03).
.venv/bin/python -m uvicorn fabric_data_plane.main:admin --host 127.0.0.1 --port 8081

# Quality gates
.venv/bin/ruff check fabric_data_plane tests
.venv/bin/python -m pytest -q
```

The cross-component test needs both packages importable, so it skips in the
data-plane environment alone and runs from one that has both:

```bash
PYTHONPATH=data-plane:control-plane \
    control-plane/.venv/bin/python -m pytest data-plane/tests/test_control_plane_interop.py
```

## Endpoints

```text
inference listener
  GET  /v1/models
  POST /v1/chat/completions
  POST /v1/completions

administrative listener
  GET  /healthz
  GET  /readyz
  GET  /admin/keys
  GET  /admin/usage
```

Readiness reports unavailable until the process holds verification keys, because
until then no token can be checked.

## Error envelope

Same shape as the control plane:

```json
{ "error": { "code": "model_not_available", "message": "...", "details": {} } }
```

Notable codes: `missing_credentials` (401), `invalid_token` (401),
`token_expired` (401), `wrong_audience` (401), `unknown_signing_key` (401),
`insufficient_scope` (403), `model_not_available` (403), `model_not_found` (404),
`model_required` (400), `invalid_json` (400), `upstream_unavailable` (503).

## Operational notes

- The ingress performs TLS termination nowhere; run it behind a proxy.
- A control-plane outage does not stop inference. `GET /admin/keys` reports
  `serving_from_cache`, refresh counts, and the last fetch error so the condition
  is visible rather than silent.
- Usage records accumulate in memory and the oldest are dropped when the buffer is
  full; `dropped` is reported. There is no exporter, so these records are not
  evidence of billable usage.
- Verified locally: `ruff` clean, 32 tests, and interop against a real
  control-plane-issued token.
