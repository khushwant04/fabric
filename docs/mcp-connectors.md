# MCP connectors

An agent run on the gateway ([`POST /v1/agent/runs`](agent-tool-loop.md)) can use the tools of
a remote [Model Context Protocol](https://modelcontextprotocol.io) server. The gateway is the
MCP client: it connects to the server, lists its tools, exposes the ones you allow to the model,
and executes the model's calls server-side. Optionally a tool waits for the user's approval
before it runs.

It is opt-in: disabled until an operator lists MCP server hosts in
`FABRIC_DP_MCP_ALLOWED_HOSTS` (`400 mcp_disabled` otherwise).

Code: `data-plane/fabric_data_plane/hosted_tools/mcp.py` (client, guards, exposure),
`hosted_tools/services.py` (per-request binding) and the approval handling in `agent_loop.py`. Tests: `data-plane/tests/test_mcp_connectors.py` (against
an in-process fake server) and `test_mcp_interop.py` (against the official MCP Python SDK's
reference server, in both SSE and JSON reply modes, and against the demo server).

## Declaring a server

```json
{
  "model": "my-model",
  "messages": [{"role": "user", "content": "Is order A-1042 delivered?"}],
  "tools": [
    {
      "type": "mcp",
      "server_label": "orders",
      "server_url": "https://mcp.example.com/mcp",
      "allowed_tools": ["get_order_status"],
      "headers": {"Authorization": "Bearer <the MCP server's own token>"},
      "require_approval": "never"
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `server_label` | 1-32 characters of letters, digits, `_`, `-`; starts with a letter; no `__`. Unique per run |
| `server_url` | The server's Streamable HTTP endpoint. `https://` required (see [Security](#security-model)) |
| `allowed_tools` | Tool names to expose (also accepted as `{"tool_names": [...]}`). Omitted: every tool the server lists, up to `FABRIC_DP_MCP_MAX_TOOLS_PER_SERVER` |
| `headers` | Sent to this server on every request, typically its credential. At most 16; protocol headers (`Host`, `Content-Type`, `Accept`, `Mcp-Session-Id`, `Cookie`, ...) cannot be set |
| `require_approval` | `"never"` (default) or `"always"`: the run pauses before each call (see [Approval](#approval)) |

The same server can be declared twice under different labels with different `allowed_tools`
and `require_approval`, for example a read-only view that runs freely and a write view that
needs approval. At most `FABRIC_DP_MCP_MAX_SERVERS_PER_RUN` (default 4) servers per run.

`mcp` declarations combine freely with function tools, built-in and HTTP executors, client
tools and [`file_search`](file-search.md).

## What happens

1. **Validation, before anything is contacted**: label, URL scheme, host allowlist, literal
   address checks, header names and values. Failures are `400` (codes below) and no request
   leaves the gateway.
2. **Connect and list** (after the model is checked): for each server, in parallel,
   `initialize` (protocol version `2025-06-18`), `notifications/initialized`, then `tools/list`
   (following `nextCursor`, at most 8 pages). A server that cannot be reached, refuses, or
   answers outside the protocol fails the request with `502 mcp_server_error`, whose message
   names the server label and the reason code, before the first model call.
3. **Expose**: each allowed tool becomes a function named `<server_label>__<tool>` (characters
   outside `[A-Za-z0-9_-]` become `_`; names longer than 64 characters, duplicate names,
   non-object schemas or schemas over 16 KiB are skipped). The description is prefixed with
   `[MCP server <label>]` and capped at 1 024 characters. The `mcp` declaration itself (URL,
   headers) is never sent to the model host.
4. **Execute**: a model call to `orders__get_order_status` becomes `tools/call` with
   `{"name": "get_order_status", "arguments": {...}}` on that server's session, in parallel with
   the turn's other tool calls. Calls to names that were not exposed are `unknown_tool` and never
   reach the server.
5. **Result**: the model receives a `role: "tool"` message:

   ```json
   {"source": "mcp:orders/get_order_status", "is_error": false,
    "content": ["...text parts..."], "structured_content": {"...": "..."}}
   ```

   Text and embedded text resources are kept; images, audio and other parts are replaced by
   `{"type": "image", "omitted": true}`. A result over `FABRIC_DP_MCP_MAX_RESULT_BYTES` (64 KiB)
   is truncated (`truncated: true` in the step). A tool that reports `isError` is passed to the
   model as such and recorded with `error: "mcp_tool_error"`; a JSON-RPC error, timeout or
   oversized reply is a tool error the model is told about (the run continues).
6. **Close**: when the request ends (completed, paused, failed or the client disconnected), each
   session is ended with an HTTP `DELETE` carrying its `Mcp-Session-Id` (best effort).

**Caching.** Within one request each server is listed exactly once. Across requests of the same
run (same `X-Fabric-Run-Id`, for example a resume after approval) the `tools/list` result is
reused for `FABRIC_DP_MCP_TOOLS_CACHE_SECONDS` (300 s; 0 disables), keyed by account, run id,
label, URL and a hash of the declared headers, so a different credential never sees another's
cached listing. Sessions are not reused across requests; a resumed run initializes a fresh one.

**Transport.** Streamable HTTP: every message is a POST with
`Accept: application/json, text/event-stream`; the reply may be one JSON-RPC message (or a
batch) or an SSE stream, in which the gateway waits for the response matching its request id
and ignores notifications. The legacy HTTP+SSE transport (separate GET stream) and stdio
servers are not supported; server-initiated requests (sampling, elicitation) are not answered.
A minimal client was written rather than adopting the SDK as a runtime dependency, so that every
request passes the same guards as HTTP tools; interop with the reference server is tested.

## Approval

With `"require_approval": "always"`, a model call to one of that server's tools is not
executed. Other tool calls from the same turn still run. The run stops with:

```json
{
  "status": "requires_action",
  "pending_tool_calls": [],
  "pending_approvals": [
    {"type": "mcp_approval_request", "tool_call_id": "call_ticket",
     "server_label": "tickets", "name": "create_refund_ticket",
     "function": "tickets__create_refund_ticket",
     "arguments": "{\"order_id\": \"A-1042\", \"reason\": \"Arrived damaged\"}"}
  ],
  "messages": ["... the full history, ending with the assistant turn ..."]
}
```

`pending_tool_calls` still lists client tools from the same turn, if any (answer those with
`role: "tool"` messages as usual). To resume, send the same request again (same `tools`) with
`messages` set to the returned history and an `approvals` list:

```json
{
  "model": "my-model",
  "tools": ["... unchanged ..."],
  "messages": ["... the returned messages ..."],
  "approvals": [
    {"type": "mcp_approval_response", "tool_call_id": "call_ticket", "approve": true}
  ]
}
```

* `approve: true`: the gateway executes the call, appends its result, and continues the loop.
* `approve: false` (optional `reason`, up to 500 characters): the gateway appends
  `{"error": "approval_denied", "message": "The user declined this call", "reason": ...}` as the
  tool result and continues, so the model can explain or choose differently. The step is
  recorded with `error: "approval_denied"`.
* Resuming without a decision for a pending call returns `requires_action` again and calls
  nothing.

Send the same `X-Fabric-Run-Id` on both requests to keep them in one run trace and reuse the
cached tool listing. The history is client-held; a caller could fabricate an assistant turn
calling a gated tool and approve it, which is no more than that caller invoking a tool it
already holds the credential for. Approval protects the *user* from the *model*, not the server
from the caller.

## Security model

* **Separate allowlist.** `FABRIC_DP_MCP_ALLOWED_HOSTS` (comma-separated hostnames) is
  independent of the HTTP tools' `FABRIC_DP_AGENT_TOOL_ALLOWED_HOSTS`. Empty disables MCP. A host
  not in it is `400 mcp_host_not_allowed`, and the check is repeated before every request.
* **SSRF guards**, the same rules as HTTP tools: loopback, link-local (including
  `169.254.169.254`), unspecified, multicast, reserved and known cloud metadata addresses
  (`metadata.google.internal`, `100.100.100.200`, `fd00:ec2::254`, ...) and `localhost` are
  refused even when allowlisted, as literals (`400`) and after DNS resolution, before every
  request (`502 mcp_server_error` / tool error `mcp_host_not_allowed`). URLs with embedded
  credentials are refused. Redirects are never followed (`redirect_not_followed`).
  *Residual risk*, shared with HTTP tools: the address is checked on the gateway's resolution
  and the connection resolves again, so a hostile DNS server for an allowlisted name could
  answer differently in between (DNS rebinding). Allowlist only names whose DNS you trust.
* **HTTPS only.** `http://` is `400 mcp_https_required` unless
  `FABRIC_DP_MCP_ALLOW_INSECURE_HTTP=true`, which is meant for local development (the demo uses
  it) and never for production.
* **Credentials.** The caller's Fabric `Authorization` header is never forwarded: the MCP
  request is built from scratch with `Content-Type`, `Accept`, `User-Agent:
  fabric-mcp-client/1`, the session and protocol-version headers, and only the `headers` the run
  declared. Declared header values are never logged, traced, put in metrics, echoed in error
  messages or responses, cached in clear (the cache key holds a SHA-256 of them), or sent to the
  model host. A dedicated HTTP client is used, never the upstream client that may carry the
  stamp's mTLS identity. Tests: `test_credentials_never_leak`.
* **Caps.** Every exchange has a wall-clock deadline (`FABRIC_DP_MCP_TIMEOUT_SECONDS`, 20 s);
  replies are read to `FABRIC_DP_MCP_MAX_RESPONSE_BYTES` (512 KiB) and anything larger is
  `mcp_response_too_large`; results handed to the model are capped separately (64 KiB); listing
  is bounded in pages, tools and schema size.
* **Untrusted output.** Tool names, descriptions, schemas and results come from a third party.
  They reach the model only as function definitions and `role: "tool"` messages wrapped in a
  JSON object labelled with their source, never as system or developer messages, and nothing in
  them changes what the gateway does (no tool can add tools, change approval, or reach another
  server). A malicious server can still try prompt injection through that text, as any tool
  result can: allowlist servers you trust, keep side-effecting tools behind `require_approval`,
  and tell the model in your system prompt that tool output is data.
* **Account scope.** Nothing about an MCP server is stored beyond the run's tool-listing cache;
  each request carries its own declaration and credential.

### Refusal codes

| Code | Status | When |
|---|---|---|
| `mcp_disabled` | 400 | `FABRIC_DP_MCP_ALLOWED_HOSTS` is empty |
| `mcp_host_not_allowed` | 400 | host not allowlisted, or a forbidden literal address / name |
| `mcp_https_required` | 400 | `http://` without the development setting |
| `invalid_tool` | 400 | malformed label, URL, `allowed_tools`, `require_approval` or headers |
| `mcp_server_error` | 502 | connecting or listing failed; the message names the label and reason |

Tool-level error codes (returned to the model; the run continues): `mcp_timeout`,
`mcp_unreachable`, `mcp_unauthorized`, `mcp_http_error`, `mcp_session_expired`,
`mcp_protocol_error`, `mcp_error` (JSON-RPC error), `mcp_response_too_large`,
`redirect_not_followed`, `mcp_tool_error` (the tool reported `isError`).

## Trace and metrics

A tool step records `executor: "hosted"`, `hosted_tool: "mcp"`, `server_label`, `mcp_tool`, `is_error`, `duration_ms`,
`result_bytes`, `truncated`, `ok`/`error`; never arguments, results or headers. Prometheus:
`fabric_dp_hosted_tool_executions_total{tool="mcp",outcome}`,
`fabric_dp_hosted_tool_duration_seconds_total`, `fabric_dp_hosted_tool_bytes_total` (shared with
the other [hosted tools](hosted-tools.md)), and
`fabric_dp_mcp_requests_total{method,outcome}` with `method` one of `initialize`, `tools/list`,
`tools/call`, `notification`. Server labels are caller-chosen and are deliberately not labels.

## Settings

| Setting (`FABRIC_DP_`) | Default | Helm (`dataPlane.mcp`) |
|---|---|---|
| `MCP_ALLOWED_HOSTS` | empty (disabled) | `allowedHosts: []` |
| `MCP_ALLOW_INSECURE_HTTP` | false | `allowInsecureHttp` |
| `MCP_TIMEOUT_SECONDS` | 20 | `timeoutSeconds` |
| `MCP_MAX_RESPONSE_BYTES` | 524288 | `maxResponseBytes` |
| `MCP_MAX_RESULT_BYTES` | 65536 | - |
| `MCP_MAX_SERVERS_PER_RUN` | 4 | - |
| `MCP_MAX_TOOLS_PER_SERVER` | 64 | - |
| `MCP_TOOLS_CACHE_SECONDS` | 300 | - |

The stamp's NetworkPolicy does not restrict egress, so the gateway can reach allowlisted hosts;
an egress firewall in front of the cluster must allow them too.

## On-stage walkthrough (no GPU)

`make demo-file-search-mcp` (see [File search](file-search.md#on-stage-walkthrough-no-gpu) for
the file half) starts `agent/scripts/demo_mcp_server.py`, a small order-desk MCP server with
three tools, and declares it twice in one agent run:

* `orders` with `allowed_tools: ["get_order_status"]`, `require_approval: "never"`;
* `tickets` with `allowed_tools: ["create_refund_ticket"]`, `require_approval: "always"`;
* the third tool, `delete_customer`, is in no `allowed_tools`: the model never sees it.

What to point at in the output:

1. `← orders__get_order_status ok  MCP server orders`: the gateway executed an MCP tool.
2. `⏸ approval needed  MCP server tickets wants to run create_refund_ticket(...)`, then
   `✓ approved` and `resume with the approval decision`: nothing ran until the user agreed.
   Run the script with `--deny` to show the model being told the user declined.
3. The final answer uses the order facts and the ticket number the MCP server returned.
4. The MCP server's own log at the end lists the header *names* it received: `authorization`
   is the demo server's key from `headers`; there is no Fabric token, and the user agent is
   `fabric-mcp-client/1`.

The demo runs the server on `127.0.0.1`, which the SSRF guard (correctly) refuses, so the demo
harness in `agent/scripts/e2e.sh` alone names it `mcp.demo.test`, resolves that name to a
documentation address for the guard and routes the connection to the local port, with
`FABRIC_DP_MCP_ALLOW_INSECURE_HTTP=true`. That shim exists only in the demo script. Against a
deployed stamp, use an HTTPS MCP server whose host is in `dataPlane.mcp.allowedHosts` and pass
its URL and key as `FABRIC_DEMO_MCP_URL` / `FABRIC_DEMO_MCP_KEY`.
