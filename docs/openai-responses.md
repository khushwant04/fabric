# Responses API through Fabric

Fabric's data plane forwards `POST /v1/responses` to the selected vLLM model host.
This source change needs a new data-plane image before the endpoint is available
on an existing stamp. The published `singletenant-20261006` image overlay does not
include it. The model host must also implement the Responses API; Fabric preserves
its request, output items, and event formats rather than converting chat responses.

Use your stamp's approved inference gateway and a **Fabric inference token** for
the account that owns the model deployment. The `model` is the deployment's public
name, its deployment UUID, or `auto` for compatible selection within that account.
The endpoint applies the same authentication, scope checks, rate limits,
concurrency limits, and backend health routing as chat completions.

## Generate text

Set these environment variables in your client environment without committing the
token to the repository:

```bash
export FABRIC_INFERENCE_BASE_URL=https://a10.inference.your-company.tld/v1
read -r -s -p 'Fabric inference token: ' FABRIC_INFERENCE_TOKEN
printf '\n'
export FABRIC_INFERENCE_TOKEN
```

```bash
curl "$FABRIC_INFERENCE_BASE_URL/responses" \
  --header "Authorization: Bearer $FABRIC_INFERENCE_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{
    "model": "qwen2-5-14b",
    "input": "Explain tensor parallelism in three sentences.",
    "instructions": "Use plain language.",
    "max_output_tokens": 256,
    "store": false
  }'
```

The response contains an `output` list. Assistant text appears in message items'
`content` entries with type `output_text`; function calls and reasoning can appear
as separate output items. Responses does not use chat completion `choices`.
Nonstreaming output reports the public deployment alias in its top-level `model`.

Clients that use the OpenAI SDK can configure the same gateway and Fabric token:

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["FABRIC_INFERENCE_BASE_URL"],
    api_key=os.environ["FABRIC_INFERENCE_TOKEN"],
)
response = client.responses.create(
    model="qwen2-5-14b",
    input="Explain tensor parallelism in three sentences.",
    max_output_tokens=256,
    store=False,
)
print(response.output_text)
```

## Stream a response

```bash
curl --no-buffer "$FABRIC_INFERENCE_BASE_URL/responses" \
  --header "Authorization: Bearer $FABRIC_INFERENCE_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{
    "model": "qwen2-5-14b",
    "input": "Write a short explanation of GPU memory.",
    "max_output_tokens": 256,
    "store": false,
    "stream": true
  }'
```

Fabric forwards vLLM's named server-sent events unchanged, including
`response.output_text.delta`, output-item events, and the terminal response event.
Read `delta` from output-text delta events to display text; inspect
`response.completed`, `response.incomplete`, or `response.failed` to finish the
request. The original stream can report the model host's served model name.
Input and output token usage in terminal events feeds Fabric's usage accounting.

```python
stream = client.responses.create(
    model="qwen2-5-14b",
    input="Write a short explanation of GPU memory.",
    max_output_tokens=256,
    store=False,
    stream=True,
)
for event in stream:
    if event.type == "response.output_text.delta":
        print(event.delta, end="", flush=True)
print()
```

## Call a function

Function tools require a model-host version, model chat template, and tool parser
that support them through Responses. For example, Qwen3-VL with vLLM v0.26 needs
automatic tool choice and the supported `hermes` parser. The A10 setup's default
vLLM v0.11.0 image rejects nonempty Responses tools for Qwen; use a newer
driver-compatible image for the example below. The new Fabric per-model tool
configuration also requires an agent/operator image containing that feature.

```python
tools = [{
    "type": "function",
    "name": "get_weather",
    "description": "Get the current weather for a city.",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    },
}]
request_input = [{"role": "user", "content": "What is the weather in Paris?"}]
response = client.responses.create(
    model="computer-use",
    input=request_input,
    tools=tools,
    tool_choice="auto",
    max_output_tokens=256,
    store=False,
)
for item in response.output:
    if item.type == "function_call":
        print(item.name, item.arguments, item.call_id)
```

Your application executes the requested function; Fabric does not execute tools.
Continue statelessly by sending the earlier input, prior output items, and matching
`function_call_output` items in the next request. Use each function call's
`call_id` to associate its result. Forward any required reasoning items unchanged.
Do not use `previous_response_id` for this workflow.

## Current limits

- Fabric uses stateless Responses: omitted or null `store` becomes `false`.
  Explicit `store: true`, `background: true`, `previous_response_id`, and
  `conversation` are rejected with HTTP 400. There are no stored-response
  retrieve or delete routes.
- Tools, `tool_choice`, structured-output `text.format`, reasoning options, and
  multimodal items are forwarded to the host. Support depends on its vLLM version
  and the selected model; the host's validation errors remain visible.
- Instructions, input, image parts, and `max_output_tokens` inform model routing.
  Image input requires a compatible vision model; a text-only deployment cannot
  accept it.
- Responses JSON bodies are limited to 16 MiB by default. Configure
  `dataPlane.responses.maxBodyBytes` in the stamp Helm values, which sets
  `FABRIC_DP_RESPONSES_MAX_BODY_BYTES` on the data-plane container. This is a
  request-size limit, not the model's token context window.

See the [Responses API reference](https://developers.openai.com/api/reference/resources/responses/methods/create)
and the [streaming Responses](https://developers.openai.com/api/docs/guides/streaming-responses)
and [function calling](https://developers.openai.com/api/docs/guides/function-calling)
guides for the wire format and client event handling. The limits above describe Fabric's
current gateway implementation, not every feature in the upstream API.
