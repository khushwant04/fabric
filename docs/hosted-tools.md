# Hosted tools: code interpreter, web fetch, web search

Fabric's server-side agent loop (`POST /v1/agent/runs`, [agent-tool-loop.md](agent-tool-loop.md))
can execute three **hosted tools** on the stamp. The model calls them like any other tool;
the gateway runs them and feeds the result back:

| Tool | Executor | What the model gets |
|---|---|---|
| `code_interpreter` | `{"type": "builtin", "name": "code_interpreter"}` | stdout, stderr (capped), exit status, timing, names of files produced |
| `web_fetch` | `{"type": "builtin", "name": "web_fetch"}` | final URL, HTTP status, title, readable text (capped) |
| `web_search` | `{"type": "builtin", "name": "web_search"}` | top-N `{title, url, snippet}` |

Every hosted tool is **off by default** and enabled per stamp. A request that declares a tool
the stamp has not enabled is refused before any model call with
`400 hosted_tool_disabled`.

The gateway never runs model-written code itself. Code runs in a separate **sandbox
service** (self-hosted, works air-gapped) or in an **E2B** microVM (connected).

## Backends at a glance

| Setting | Value | Isolation / reach | Air-gapped? | Data leaves the cluster? |
|---|---|---|---|---|
| `FABRIC_DP_CODE_INTERPRETER_BACKEND` | `sandbox` | Fabric sandbox pod: process isolation, rlimits, no-egress NetworkPolicy, optional gVisor/Kata runtime class | yes | no |
| | `e2b` | E2B sandbox per run (Firecracker microVM) | only with self-hosted E2B | **yes** to E2B cloud, unless E2B is self-hosted (`FABRIC_DP_E2B_DOMAIN`) |
| `FABRIC_DP_WEB_BACKEND` | `direct` | the gateway fetches pages itself; search via a generic HTTP API template | fetch: only intranet hosts you list; search: needs a local index | queries go to the API you configure |
| | `searxng` | direct fetch; search via self-hosted SearXNG | only if SearXNG has a local index/engines it can reach | to SearXNG's upstream engines |
| | `firecrawl` | fetch (scrape → markdown) and search through self-hosted Firecrawl | only with a local index for search | Firecrawl reaches the target |

**Recommendation.** For untrusted, multi-tenant code use the `e2b` backend (a VM boundary
per run) or run the sandbox with a sandboxed runtime class (`sandbox.runtimeClassName:
gvisor` or `kata`). Use the plain process sandbox for single-tenant stamps, trusted users,
or air-gapped sites where neither is available, and understand what it does not stop
(below). The trade-off for E2B cloud is that code and any data the model puts in it leave
your cluster.

`web_search` cannot work in a fully air-gapped site unless a local index exists (for
example SearXNG over an internal search engine). `web_fetch` air-gapped is useful only
for internal hosts you allowlist explicitly.

## Declaring the tools

```json
{
  "model": "my-model",
  "messages": [{"role": "user", "content": "Plot p95 latency from https://wiki.internal/q3"}],
  "tools": [
    {"type": "function", "function": {"name": "web_fetch"},
     "x-fabric-executor": {"type": "builtin", "name": "web_fetch"}},
    {"type": "function", "function": {"name": "code_interpreter"},
     "x-fabric-executor": {"type": "builtin", "name": "code_interpreter"}}
  ]
}
```

`description` and `parameters` may be omitted; the gateway fills in its own (the model sees
`code: string` for `code_interpreter`, `url: string` for `web_fetch`, `query: string` and
optional `count` for `web_search`). Your own description or parameters are kept if given.
`x-fabric-executor` never reaches the model.

### Run response additions

* Tool steps of hosted tools carry `executor: "hosted"`, `hosted_tool`, and size/outcome
  fields: `code_interpreter` → `backend, exit_code, timed_out, code_bytes, stdout_bytes,
  stderr_bytes, files, file_bytes, backend_ms`; `web_fetch` → `backend, http_status,
  fetched_bytes, text_chars, redirects`; `web_search` → `backend, results, query_chars`.
  **Never** code, page text, queries, URLs or results. The same fields are on the streamed
  `tool.completed` events.
* `files`: images produced by `code_interpreter` (matplotlib PNGs and other images),
  `[{tool_call_id, name, mime_type, bytes, content_base64}]`, capped per file
  (`FABRIC_DP_CODE_INTERPRETER_MAX_FILE_BYTES`, 2 MiB) and per run (`..._MAX_RUN_FILE_BYTES`,
  8 MiB); beyond the caps an entry has `omitted: "size_cap"` instead of content. Other
  produced files are listed without content. Send `"include_files": false` to get names
  and sizes only.

## code_interpreter

### Sessions

State is keyed by `(account, run id)`: files written in one call are visible to later
calls in the same run (with E2B the Python interpreter state also persists). The session is
dropped when the run ends (`completed`, `failed`, `max_steps_reached`, or the client
disconnects); it survives `requires_action`, which is resumed under the same run id.
Abandoned sessions expire after an idle TTL (sandbox: `FABRIC_SANDBOX_SESSION_TTL_SECONDS`,
30 min; E2B: `FABRIC_DP_E2B_IDLE_TTL_SECONDS`, 10 min, plus E2B's own lifetime limit).
Backends only see an opaque SHA-256 of account and run id.

### Sandbox backend: security model

The sandbox service (`sandbox/`, image `deploy/images/sandbox.Dockerfile`) accepts calls
only with the shared secret in `X-Fabric-Sandbox-Token` (compared in constant time); it
refuses to start without one. Layers, and whether each is enforced by code or by the
deployment:

| Layer | Where | What it does | What it does not do |
|---|---|---|---|
| Fresh interpreter per execution | code | `python -I -S` in a new session/process group; no `PYTHON*` variables, no user site, no `sitecustomize` | — |
| Scrubbed environment | code | the child gets a fixed allowlist of variables; the service's token never reaches it | — |
| Non-dumpable processes | code | the service and every runner call `prctl(PR_SET_DUMPABLE, 0)`: same-uid code cannot read their `/proc/<pid>/environ`, memory or cwd, or ptrace them | does not stop same-uid signals (a hostile execution could kill a concurrent one: DoS, not disclosure) |
| rlimits (hard = soft) | code | CPU seconds (30), address space (2 GiB), file size (64 MiB), open files (256), processes (256, per uid), no core dumps | RLIMIT_NPROC counts all tasks of the uid, so it is a fork-bomb brake, not a per-run quota |
| Wall-clock timeout | code | SIGKILL to the whole process group at the deadline; the group is also killed after every normal exit | a process that calls `setsid()` leaves the group; it still has no network, its rlimits, and dies with the pod |
| Output caps | code | stdout/stderr each kept to 32 KiB, the rest drained | — |
| Per-run directories | code | name = HMAC(per-process key, session id) under a non-listable (0711) parent; total size cap per session (256 MiB, new files removed when exceeded); idle TTL; produced images are read with `O_NOFOLLOW` and refused if hard-linked | other executions of the same uid could read a directory whose name they learn (they cannot list or compute it) |
| Concurrency | code | `FABRIC_SANDBOX_MAX_CONCURRENT` per replica; waits up to 10 s then `429` → `sandbox_busy` | — |
| Non-root | code + image | image runs as uid 10001; the service refuses to run user code as root (`FABRIC_SANDBOX_RUN_AS_UID` can drop to another uid when started as root) | — |
| No network | **deployment** | NetworkPolicy: `egress: []` (no DNS either), ingress only from the stamp gateway pod | requires a CNI that enforces NetworkPolicy; without one the policy is silently ignored |
| Filesystem | **deployment** | read-only root filesystem; one emptyDir (`sizeLimit`) is the only writable path | `/tmp` is not writable; code must use the working directory |
| Capabilities, seccomp | **deployment** | `drop: [ALL]`, `allowPrivilegeEscalation: false`, seccomp `RuntimeDefault`, no service-account token | — |
| Kernel boundary | **deployment, optional** | `sandbox.runtimeClassName: gvisor` (runsc) or `kata` | not enabled by default: the cluster must offer the runtime class |

> **Process isolation plus NetworkPolicy is not equivalent to a VM boundary.** All
> executions on a replica share one kernel and one uid. A kernel exploit, or a bug in the
> layers above, can cross runs. For untrusted multi-tenant code, set a gVisor/Kata
> `runtimeClassName`, use the `e2b` backend, or give each tenant its own stamp.

The service's packages and the kernel environment (numpy 2.1.3, pandas 2.2.3,
matplotlib 3.9.2) are separate virtualenvs in the image, so user code does not import the
service's packages by accident; both are readable, so this is hygiene, not a boundary.
Matplotlib uses the Agg backend; open figures are saved as `figure_N.png` when the code did
not save a PNG itself.

### E2B backend

`FABRIC_DP_CODE_INTERPRETER_BACKEND=e2b` uses the official `e2b-code-interpreter` SDK
(pinned 2.11.10, async API; the `e2b` extra of the data-plane package, installed in the
default image). One sandbox per run, created on the first call, reused, killed when the run
ends or idles. Settings:

| Setting | Default | |
|---|---|---|
| `FABRIC_DP_E2B_API_KEY` | — | from a Secret only. Never logged, traced or echoed: SDK errors are reported as `sandbox_unavailable` / `sandbox_error` with no SDK text |
| `FABRIC_DP_E2B_TEMPLATE` | E2B default | a template with your packages |
| `FABRIC_DP_E2B_DOMAIN` | E2B cloud | a self-hosted E2B |
| `FABRIC_DP_E2B_SANDBOX_TIMEOUT_SECONDS` | 900 | lifetime E2B enforces |
| `FABRIC_DP_E2B_IDLE_TTL_SECONDS` | 600 | gateway-side idle kill |
| `FABRIC_DP_E2B_MAX_SANDBOXES` | 8 | per gateway; beyond it `sandbox_busy` |
| `FABRIC_DP_E2B_ALLOW_INTERNET` | false | outbound internet from the sandbox |

The caller's Fabric token is never sent to E2B; sandbox metadata holds only a truncated
opaque run hash. PNG/JPEG results become `files`; the main expression result is returned
as `result`. E2B does not report which files a call wrote, so only image results are
listed as files.

## web_fetch

The model passes `url`. Policy, applied to the URL, to every redirect hop and to every URL
handed to Firecrawl:

* `http`/`https`, no credentials in the URL;
* host matches `FABRIC_DP_WEB_FETCH_ALLOWED_HOSTS`: listed names, `*.suffix` entries, or `*`
  (any host);
* loopback, link-local, unspecified, multicast, reserved and cloud-metadata addresses
  (169.254.169.254, `fd00:ec2::254`, 100.100.100.200, `metadata.google.internal`, …) are
  refused always, as literals and after DNS;
* a host admitted only by `*` must resolve to **globally routable** addresses: RFC 1918,
  CGNAT, ULA and other private ranges need an explicit entry (an intranet wiki);
* the name is resolved once, every address is checked, and the connection is **pinned** to
  the vetted address (Host header and TLS SNI/verification keep the name; no connection
  reuse), so DNS rebinding cannot move the request;
* redirects are followed by hand, at most `FABRIC_DP_WEB_FETCH_MAX_REDIRECTS` (3), each hop
  re-checked;
* no cookies are stored or sent, no caller header is forwarded, no proxy from the
  environment; `User-Agent: Fabric-WebFetch/1 (hosted agent tool)`; robots.txt is not
  consulted;
* bytes read are capped (`FABRIC_DP_WEB_FETCH_MAX_BYTES`, 2 MiB, after decompression), text
  returned is capped (`..._MAX_CHARS`, 20 000), total time is capped
  (`..._TIMEOUT_SECONDS`, 15);
* HTML is reduced to readable text with a standard-library extractor (scripts, styles,
  head, SVG and iframes dropped; blocks and table cells kept); `text/*`, JSON and XML are
  returned as text; other content types are refused.

With `FABRIC_DP_WEB_BACKEND=firecrawl`, the gateway checks the target exactly as above
(including DNS) and then calls Firecrawl's scrape endpoint for markdown. Firecrawl resolves
and connects itself, so it could still be redirected or rebound to an internal address:
**give Firecrawl its own egress NetworkPolicy that blocks private ranges and metadata**.

## web_search

| Backend | Request | Results from |
|---|---|---|
| `searxng` | `GET {FABRIC_DP_WEB_SEARCH_URL}/search?q=…&format=json` (enable the JSON format in SearXNG's `settings.yml`) | `results[].title/url/content` |
| `direct` | `GET` the template in `FABRIC_DP_WEB_SEARCH_URL`, `{query}` and `{count}` substituted; optional key in `FABRIC_DP_WEB_SEARCH_API_KEY` sent as header `FABRIC_DP_WEB_SEARCH_API_KEY_HEADER` | `results[]` (or a top-level list) with `title`, `url`, `snippet`/`content`/`description` |
| `firecrawl` | `POST {FABRIC_DP_FIRECRAWL_URL}{FABRIC_DP_FIRECRAWL_SEARCH_PATH}` `{query, limit}` | v1 `data[]` or v2 `data.web[]` |

`count` is capped by `FABRIC_DP_WEB_SEARCH_MAX_RESULTS` (5). Search endpoints are
operator-configured services: they may be in-cluster or on loopback, but a URL pointing at a
link-local or metadata address is refused at startup. Firecrawl paths default to
`/v1/scrape` and `/v1/search` and are configurable (`/v2/...` for newer Firecrawl); an
optional key (`FABRIC_DP_FIRECRAWL_API_KEY`) is sent as `Authorization: Bearer`. Fabric's
chart does not deploy Firecrawl or SearXNG; follow their own self-hosting guides.

## Configuration reference (gateway, `FABRIC_DP_` prefix)

| Setting | Default |
|---|---|
| `CODE_INTERPRETER_ENABLED` / `WEB_FETCH_ENABLED` / `WEB_SEARCH_ENABLED` | false |
| `CODE_INTERPRETER_BACKEND` | `sandbox` (`e2b`) |
| `CODE_INTERPRETER_URL`, `CODE_INTERPRETER_TOKEN` | — (required for `sandbox`) |
| `CODE_INTERPRETER_TIMEOUT_SECONDS` | 30 |
| `CODE_INTERPRETER_MAX_OUTPUT_BYTES` | 32768 (each of stdout/stderr) |
| `CODE_INTERPRETER_MAX_FILE_BYTES`, `CODE_INTERPRETER_MAX_RUN_FILE_BYTES` | 2 MiB, 8 MiB |
| `E2B_*` | see above |
| `WEB_BACKEND` | `direct` (`searxng`, `firecrawl`) |
| `WEB_FETCH_ALLOWED_HOSTS` | — (required when web_fetch is enabled) |
| `WEB_FETCH_TIMEOUT_SECONDS`, `_MAX_BYTES`, `_MAX_CHARS`, `_MAX_REDIRECTS` | 15, 2 MiB, 20000, 3 |
| `WEB_SEARCH_URL`, `WEB_SEARCH_API_KEY`, `WEB_SEARCH_API_KEY_HEADER`, `WEB_SEARCH_MAX_RESULTS`, `WEB_SEARCH_TIMEOUT_SECONDS` | —, —, `Authorization`, 5, 10 |
| `FIRECRAWL_URL`, `FIRECRAWL_API_KEY`, `FIRECRAWL_SCRAPE_PATH`, `FIRECRAWL_SEARCH_PATH`, `FIRECRAWL_TIMEOUT_SECONDS` | —, —, `/v1/scrape`, `/v1/search`, 30 |

An enabled tool that is not configured stops the gateway at startup with a message naming
the missing setting. Hosted tool calls count against the run's `max_tool_calls` like any
server tool.

Sandbox service (`FABRIC_SANDBOX_` prefix): `TOKEN` (required, ≥16 chars), `ROOT_DIR`,
`PYTHON`, `DEFAULT_TIMEOUT_SECONDS` (30), `MAX_TIMEOUT_SECONDS` (120), `CPU_SECONDS` (30),
`MEMORY_BYTES` (2 GiB), `FILE_SIZE_BYTES` (64 MiB), `OPEN_FILES` (256), `MAX_PROCESSES`
(256), `MAX_OUTPUT_BYTES` (32 KiB), `MAX_CODE_BYTES` (256 KiB), `MAX_RETURNED_FILE_BYTES`
(2 MiB), `MAX_RETURNED_FILES` (10), `SESSION_TTL_SECONDS` (1800), `SESSION_MAX_BYTES`
(256 MiB), `MAX_SESSIONS` (1000), `MAX_CONCURRENT` (4), `QUEUE_TIMEOUT_SECONDS` (10),
`RUN_AS_UID`/`RUN_AS_GID`, `ALLOW_ROOT` (false; throwaway dev containers only).

## Helm (`deploy/helm/fabric-stamp`)

```yaml
hostedTools:
  codeInterpreter:
    enabled: true
    backend: sandbox            # or e2b
    e2b: {existingSecret: e2b-api-key}   # e2b only; key "api-key"
  webFetch: {enabled: true, allowedHosts: "*,wiki.internal"}
  webSearch: {enabled: true}
  web:
    backend: searxng            # direct | searxng | firecrawl
    searchUrl: http://searxng.search.svc:8080
    firecrawl: {url: "", existingSecret: ""}
sandbox:
  runtimeClassName: gvisor      # strongly recommended for untrusted code
  replicas: 1
  limits: {maxConcurrent: 4, cpuSeconds: 30, memoryBytes: 2147483648}
  storage: {sizeLimit: 2Gi}
```

With `backend: sandbox` the chart renders the sandbox Deployment, Service, a generated
shared-secret Secret (kept across upgrades; or `sandbox.token.existingSecret`) wired into
both pods, and the NetworkPolicy above. Sandbox pods use their own
`app.kubernetes.io/name` so no stamp selector matches them. Misconfigurations (e2b without
a Secret, web_fetch without `allowedHosts`, firecrawl without a URL) fail rendering.
`deploy/scripts/test_hosted_tools_chart.py` checks the rendering.

Build the image with `docker build -f deploy/images/sandbox.Dockerfile -t fabric/sandbox .`.
The data-plane image takes `--build-arg EXTRAS=` to leave out the E2B SDK.

## Metrics

Gateway: `fabric_dp_hosted_tool_executions_total{tool, outcome}` (`ok`, `code_error`,
`timeout`, `error`), `fabric_dp_hosted_tool_duration_seconds_total{tool}`,
`fabric_dp_hosted_tool_bytes_total{tool, kind}` (`result`, `fetched`, `file`, `code`).
Sandbox (`/metrics`, unauthenticated, counts only): `fabric_sandbox_executions_total`,
`_timeouts_total`, `_failures_total`, `_busy_total`, `fabric_sandbox_active_executions`,
`fabric_sandbox_sessions`.

## Limits and known gaps

* The sandbox is process isolation; see the warning above. NetworkPolicy needs an
  enforcing CNI. Same-uid executions on one replica can signal each other.
* Sessions live in one sandbox replica's memory and disk. With several replicas the
  Service may send a run's second call to another replica, which will not have its files:
  run one replica, or put session affinity in front (not provided).
* Gateway-side E2B state is per gateway process; a gateway restart orphans sandboxes until
  E2B's own timeout (`E2B_SANDBOX_TIMEOUT_SECONDS`) kills them.
* E2B sandbox creation holds a gateway-wide lock (one creation at a time).
* `web_fetch` does no JavaScript rendering (use Firecrawl for that) and does not consult
  robots.txt. There is no egress-proxy support for direct fetch.
* Firecrawl performs its own DNS and redirects; protect it with its own egress policy.
* Only Python is offered by the sandbox.

## On-stage walkthrough (`make demo-hosted-tools`, no GPU)

1. `make demo-hosted-tools` starts the local platform (control plane, Go agent, data plane)
   with a scripted model host, the **sandbox as a local process** with the pinned
   scientific stack, and a SearXNG-compatible stub. The demo web page is served to
   `web_fetch` through an injected transport at `http://fabric-demo.test/` (resolving to
   203.0.113.50), because a real page on 127.0.0.1 would be refused by design.
2. One streamed `POST /v1/agent/runs`:
   `web_search` → 2 results; `web_fetch` follows one redirect (re-checked) and returns the
   report; the parallel `web_fetch` to `http://169.254.169.254/...` (what a prompt-injected
   model would try) is **refused** with `host_not_allowed`; `code_interpreter` loads the
   table into pandas and writes `latency.csv`; a second `code_interpreter` call reads that
   CSV (session persistence) and plots `latency.png`.
3. The answer names the region over the SLO; the PNG is saved from the run response's
   `files` to `/tmp/fabric-demo/latency.png` (open it on screen).
4. The trace shows only tool names, durations, outcomes and sizes; the script checks it
   contains no code or page text. Gateway and sandbox metrics are printed.
5. Optional: with `FABRIC_DP_E2B_API_KEY` and/or `FABRIC_DEMO_FIRECRAWL_URL` set (and
   optionally `FABRIC_DEMO_WEB_URL`), a second pass restarts the gateway on E2B / Firecrawl
   and runs the same agent. Without them the step prints "skipped".

Talking points: the gateway never executes code; disabled tools are refused at request
validation; secrets (sandbox token, E2B key, Firecrawl key) come from Secrets and never
appear in traces; and be explicit that the local sandbox is process isolation, with gVisor
or E2B for hostile multi-tenant code.
