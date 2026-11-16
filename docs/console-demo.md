# Console demo

`make demo-console` runs the Fabric console (`v1/`) locally against a real control plane,
with a demo account that already has stamps, deployments in different states, access
records, usage and an audit trail. It needs no Auth0 tenant, cloud account or GPU.

```bash
make demo-console              # runs until Ctrl-C
make demo-console-screenshots  # same setup, then writes docs/screenshots/console/ and exits
```

When it is ready it prints:

```
Console          http://localhost:3000
Sign in          click "Sign in"; the demo identity provider signs you in as
                 Maya Chen (maya.chen@northstar-labs.example), owner of Northstar Labs
```

On the console, click **Continue with SSO**. The demo identity provider signs you in
straight away, with no password prompt. On **Choose your workspace**, open the account
marked *Owner*. That is Northstar Labs. The other account (*Developer*) is Northstar Research,
which is there so the account switcher has a second entry.

## Requirements

- What `make setup` installs: the control-plane and data-plane Python environments, plus
  `npm ci` in `v1/`. The target runs `setup-python` itself and installs console
  dependencies if they are missing.
- Go, to build the agent and the collector.
- `openssl` and `curl`.
- Free ports 3000, 8080, 8090, 8443, 8444 and 9009. Each one can be changed: see the
  `DEMO_*_PORT` variables at the top of `scripts/demo-console/run.sh`.
- For screenshots only: Playwright with Chromium (`require('playwright')` must resolve).

The console is built with `next build` and served with `next start`, which takes a few minutes
on the first start. `DEMO_CONSOLE_MODE=dev make demo-console` runs `next dev` instead, which
starts faster and hot-reloads. In dev mode the playground uses the HTTP data-plane URL, and
Next's development indicator appears in the corner.

## What is real and what is mocked

| Component | Real or mocked | Notes |
|---|---|---|
| Console (`v1/`) | **real**, unmodified | Production build. It uses its real Auth0 SDK, configured through environment variables only. |
| Control plane | **real** | FastAPI with SQLite, migrated from scratch on every start. It verifies the identity provider's tokens with its usual Auth0 verifier. |
| Stamp agent | **real** | The Go `fabric-agent`. It enrolls each stamp with a single-use token, pulls desired state, writes `deployments.json` and reports status. |
| Data plane | **real** | One gateway per stamp, loaded from the file that stamp's agent wrote. Inference listeners use HTTPS. |
| Collector | **real** | The Go `fabric-collector`. It drains each data plane's usage and forwards it every 30 s. |
| Identity provider | **mock** | `scripts/demo-console/mock_oidc.py`. A minimal OIDC provider (discovery, JWKS, `/authorize`, `/oauth/token` with code + PKCE and refresh, `/userinfo`, logout). It stands in for Auth0. |
| Model host | **mock** | `scripts/demo-console/model_host.py`. An OpenAI-compatible stub that streams canned answers and reports token usage the way vLLM does. With `tools` it plays a scripted tool-using agent for the agent playground. |

Two inputs can only come from hardware, so the seeding script sends them over the same
stamp-authenticated APIs a production stamp uses, with each stamp's own agent credential:

- **GPU inventory.** Without a Kubernetes cluster, the agent runs with `--gpus N`. That
  reports a device count but no device model. The script sends the heartbeat a measuring
  agent would send: product, memory and compute capability.
- **Model-host readiness.** In a cluster, the operator observes the pods and the agent
  forwards what it sees. Here the script reports the operator-style conditions: Applied,
  Available and Progressing. This is what makes one deployment *Ready*, another *Deploying*
  and another *Failed*.

Nothing is written to the database directly. Every other record is created through the public
APIs by the person or machine that would create it.

## The demo account

`scripts/demo-console/seed.py` creates everything:

- **Northstar Labs**, owned by Maya Chen. The other members are Priya Raman (admin), Daniel
  Okafor and Sofia Lindqvist (developers) and Tom Becker (viewer). Each one signs in once
  through the mock provider before being added, the way a real first sign-in records a user.
- **Stamps:**
  - `aks-gpu`: AKS, eastus2, 2x Tesla T4 and 2x NVIDIA A10.
  - `gcp-rtx6000`: GKE, us-central1, 2x NVIDIA RTX PRO 6000 Blackwell Server Edition.
  - `vm-gpu`: k3s, westeurope, 1x NVIDIA A10 and 1x Tesla T4.

  There is also one outstanding enrollment token.
- **Deployments.** Small Qwen3.5 models served by vLLM:

  | Deployment | Model | Stamp | State |
  |---|---|---|---|
  | `support-assistant` | `qwen3.5-2b`, 2 replicas | aks-gpu | Ready |
  | `chat-fast` | `qwen3.5-0.8b` | aks-gpu | Ready |
  | `assistant-large` | `qwen3.5-2b-large` (cascade upper tier) | gcp-rtx6000 | Ready |
  | `code-review-bot` | `qwen3.5-2b-code` | gcp-rtx6000 | Deploying |
  | `batch-summarizer` | `qwen3.5-0.8b-batch` | vm-gpu | Updating (generation 2 not yet observed) |
  | `long-context-eval` | `qwen3.5-2b-longctx` | vm-gpu | Failed (KV cache does not fit on a T4) |
  | `research-pool` | `qwen3.5-2b-research`, 4 replicas | not placed | Waiting |
- **Access:**
  - Four service principals. `legacy-etl` is disabled.
  - Six API keys, with and without service principals and expiry. `legacy-notebook` is revoked.
  - An OIDC identity provider registration for `login.northstar-labs.example`.
- **Traffic:** about 340 chat completions, some streamed, sent through the data planes with
  three different API keys and forwarded by the collector. These fill the Usage page and the
  dashboard counters.
- **Agent runs:** three runs on aks-gpu for **Agents > Runs**: `trip-planner-7f2c` (a
  server-side tool loop with one tool-guard repair), `nightly-eval-0412` (stopped by its
  step budget, one refusal) and `support-ticket-881` (overshoots its token budget). When a
  request declares tools, the stub model host plays a scripted agent (see
  [Console: Agents](console-agents.md)).
- **Audit trail:** every change above is recorded as it happens.

## How sign-in works without Auth0

The console's Auth0 SDK only accepts a bare host name as `AUTH0_DOMAIN`: no port, no
`localhost`, no IP address. It always talks to `https://<domain>/`. The demo provider therefore
uses its own name, `idp.fabric-demo.test`, and a certificate from a throwaway CA. Nothing
resolves that name, so two small preloads map it to `127.0.0.1:8443` for the processes that
need it:

- `scripts/demo-console/resolve-idp.cjs` for the console's Node process, through
  `NODE_OPTIONS=--require`.
- `scripts/demo-console/pyshim/sitecustomize.py` for the control plane, through `PYTHONPATH`.

TLS is still verified against the name, using `NODE_EXTRA_CA_CERTS` and `SSL_CERT_FILE`
pointed at the throwaway CA. No insecure flags are set. The browser-facing endpoints
(`/authorize` and logout) are served on plain `http://localhost:8444`, so your browser needs
neither the CA nor a hosts-file entry.

The CA, the certificates, the provider's signing key, the client secret, the session secret,
the credential pepper and the database are all generated in a temporary directory on every
start and deleted on exit. Set `DEMO_KEEP_WORK=1` to keep the directory for its logs. Nothing
is committed.

## Limitations

- The playground sends every request to one gateway (`FABRIC_INFERENCE_URL`), which is
  aks-gpu's data plane. Compare `support-assistant` and `chat-fast` there. Choosing
  `assistant-large`, which runs on gcp-rtx6000, fails, because that gateway does not serve it.
- Answers come from the stub model host. They are canned, and only their length depends on the
  model name. Token counts and streaming timing are real measurements of the stub.
- Deployment endpoints such as `https://aks-gpu.gw.northstar-labs.example/v1` are the
  `--inference-url` each agent reports. They are not reachable.
- Usage totals reset on every start, because the database is created fresh each time.
