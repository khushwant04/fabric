# Model cascade

`model: "cascade"` answers a chat completion with the account's smallest deployment first and
moves to a larger one only when the answer fails a cheap, explainable check. Easy requests stay
on cheap capacity; hard or structured ones still get answered.

It is opt-in: only a request whose `model` is literally `cascade` takes this path. Any other
model name, including `auto`, is served exactly as before. An account that already has a real
deployment aliased `cascade` keeps that alias's meaning.

Code: `data-plane/fabric_data_plane/cascade.py`. Tests: `data-plane/tests/test_cascade.py`.

## Where it applies

* `POST /v1/chat/completions` with `model: "cascade"`.
* `POST /v1/agent/runs` with `model: "cascade"`: every model call of the loop cascades
  independently, and each model step reports `cascade: {path, escalations, reasons}`
  ([agent tool loop](agent-tool-loop.md)).

## The ladder

The candidates are the calling account's own deployments; another account's deployments are
never considered. They are ordered small to large by this rule:

1. If any of the account's deployments declares a cascade tier, the ladder is exactly those
   deployments, lowest tier first (ties by alias). Deployments without a tier stay out.
2. Otherwise the ladder is every chat-capable deployment with `capabilities.auto_enabled`, by
   `capabilities.priority` **ascending** (ties by alias). Give the small model a low priority
   and the large one a high priority.

Both travel from the control plane's deployment spec through the agent to `deployments.json`.
Set the tier as `spec.capabilities.cascade_tier` (an integer from -1000 to 1000, or unset) when
creating or updating a deployment, or under **Routing** in the console's deployment form; the
agent renders it as the entry's top-level `cascade_tier`. Use it when the cascade ladder should
differ from the `model: "auto"` preferences that `priority` expresses.

The ladder is cut to `max_escalations + 1` tiers. An account with no eligible deployment gets
`404 cascade_not_configured`.

## Escalation checks

An attempt is rejected, and the next tier tried, when its answer:

| Reason | Check |
|---|---|
| `length` | `finish_reason` is `length` (the answer was cut off) |
| `empty` | no content and no tool calls |
| `invalid_tool_call` | a tool call's `arguments` is not a JSON object |
| `unknown_tool` | a tool call names a tool the request did not declare |
| `invalid_json` | `response_format` is `json_object`/`json_schema` and the content is not JSON (an object, for `json_object`) |
| `schema_mismatch` | the content does not validate against `response_format.json_schema.schema` |
| `upstream_unavailable` / `upstream_error` | the tier's host could not answer (`5xx`) |

The checks are deliberately not quality judgements: each one is something a client would
otherwise have to detect and retry itself. The schema validator supports `type`, `enum`,
`const`, `properties`, `required`, `additionalProperties`, `items`, `min/maxItems`,
`min/maxLength`, `minimum/maximum` and `anyOf/oneOf/allOf`; an unsupported keyword is ignored,
so it can only under-report.

Tool calls are first checked by the [tool-call guard](agent-runs.md), which may re-ask the same
tier (a billed retry) before the cascade sees the answer; the cascade escalates when the
repaired answer is still invalid. Send `X-Fabric-Tool-Guard: off` to escalate immediately.

A `4xx` from a tier (for example a rate limit or a rejected request) is returned as is, not
escalated. The last tier's answer is returned whatever it is; the headers say why it was
reached.

## Response

The body is the final attempt's ordinary chat completion; its `model` is the alias of the tier
that produced it. Headers:

| Header | Example |
|---|---|
| `X-Fabric-Cascade-Path` | `small-model>large-model` |
| `X-Fabric-Cascade-Escalations` | `1` |
| `X-Fabric-Cascade-Reason` | `schema_mismatch` (one per escalation, joined with `>`), `accepted` when the first answer passed, or `streaming` |

## Metering and limits

Every attempt is a separate internal chat completion through the gateway's proxy: each is
admitted against the account's limits, metered as usage on its own deployment, charged to the
account's token rate and, when `X-Fabric-Run-Id` is sent, counted as a step of the agent run.
A cascade that escalates twice costs three model calls. The caller pays for the attempts it
caused, which is why escalation is bounded.

| Control | Default | Meaning |
|---|---|---|
| `FABRIC_DP_CASCADE_MAX_ESCALATIONS` | 2 | Operator ceiling on escalations per request |
| `"cascade": {"max_escalations": N}` in the body | the ceiling | A request may ask for fewer; the field is never forwarded to a model host |

## Streaming

`stream: true` with `model: "cascade"` is routed to the first tier and never escalated: a
streamed answer has already reached the client by the time it could be judged. The response
carries `X-Fabric-Cascade-Path: <first tier>` and `X-Fabric-Cascade-Reason: streaming`.

## On-stage demo (no GPU)

```bash
make demo-cascade
```

This starts the real control plane, Go agent and data plane locally, creates two deployments
through the control plane (`e2e-model` with `capabilities.priority: -10`, `e2e-model-large`
with `10`), places both, and runs `examples/python/scripts/cascade_demo.py` against a
deterministic stub host (`agent/scripts/agent_loop_demo_host.py`):

1. **Easy question.** "What is the capital of Portugal?" Path `e2e-model`, reason `accepted`:
   the small model answered and nothing else was spent.
2. **Long reasoning.** A step-by-step word problem. The small tier runs out of tokens, so the
   path is `e2e-model > e2e-model-large`, reason `length`.
3. **Structured output.** A `json_schema` request. The small tier's JSON misses required fields:
   reason `schema_mismatch`, answered by `e2e-model-large`, and the JSON validates.

The script ends with the gateway's per-deployment request counters, showing that the rejected
small-tier attempts were metered too. Against a real host:
`FABRIC_E2E_UPSTREAM=http://127.0.0.1:8000 FABRIC_E2E_RELEASE=<small> FABRIC_E2E_RELEASE_LARGE=<large> make demo-cascade`.
