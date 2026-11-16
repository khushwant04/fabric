# Anthropic Messages API through Fabric

Fabric's data plane serves `POST /v1/messages`, the Anthropic Messages API, in front
of the same vLLM model hosts that serve `/v1/chat/completions`. The gateway
translates each request into a chat-completions request, sends it to the host's
`/v1/chat/completions`, and translates the reply back. The model host does not
need its own `/v1/messages` endpoint.

The translated request goes through the chat-completions path unchanged, so
`/v1/messages` gets the same authentication, account ownership checks,
`model: "auto"` routing, rate, token and concurrency limits, backend retries and
health routing, prefix-aware routing, and usage metering. The usage spool records
one row per request, with the host's own token counts.

This source change needs a new data-plane image before the endpoint is available
on an existing stamp.

## Credentials and headers

Use a **Fabric inference token** for the account that owns the deployment. The
gateway accepts it in either header:

| Header | Sent by |
|---|---|
| `x-api-key: <token>` | the Anthropic SDK constructed with `api_key=` |
| `Authorization: Bearer <token>` | the Anthropic SDK constructed with `auth_token=`, and most HTTP clients |

When both are present, `Authorization` is used. Neither header is forwarded to
the model host. `anthropic-version` and `anthropic-beta` are accepted and ignored:
they are not forwarded, and beta features are not enabled by them.

The `model` is the deployment's public name, its deployment UUID, or `auto`.
Replies report the public name of the deployment that answered, including after
`auto` selection.

## Send a message

Set the gateway root (without `/v1`) and the token in your client environment,
without committing the token to the repository:

```bash
export FABRIC_GATEWAY_URL=https://a10.inference.your-company.tld
read -r -s -p 'Fabric inference token: ' FABRIC_INFERENCE_TOKEN
printf '\n'
export FABRIC_INFERENCE_TOKEN
```

```bash
curl "$FABRIC_GATEWAY_URL/v1/messages" \
  --header "x-api-key: $FABRIC_INFERENCE_TOKEN" \
  --header 'anthropic-version: 2023-06-01' \
  --header 'content-type: application/json' \
  --data '{
    "model": "launch-model",
    "max_tokens": 256,
    "system": "Use plain language.",
    "messages": [
      {"role": "user", "content": "Explain tensor parallelism in three sentences."}
    ]
  }'
```

The Anthropic Python SDK takes the gateway root as `base_url`:

```python
import os
from anthropic import Anthropic

client = Anthropic(
    base_url=os.environ["FABRIC_GATEWAY_URL"],
    api_key=os.environ["FABRIC_INFERENCE_TOKEN"],
)
message = client.messages.create(
    model="launch-model",
    max_tokens=256,
    system="Use plain language.",
    messages=[{"role": "user", "content": "Explain tensor parallelism in three sentences."}],
)
print(message.content[0].text)
print(message.stop_reason, message.usage)
```

## Stream a message

Add `"stream": true`. Fabric sends Anthropic's named server-sent events:
`message_start`, then `content_block_start` / `content_block_delta` /
`content_block_stop` for each block (`text_delta`, `thinking_delta` and
`input_json_delta` deltas), then `message_delta` with the stop reason and token
usage, and `message_stop`. In `message_start`, `usage` is zero because the host
reports usage only at the end; read the counts from `message_delta`.

```python
with client.messages.stream(
    model="launch-model",
    max_tokens=256,
    messages=[{"role": "user", "content": "Write a short explanation of GPU memory."}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
    final = stream.get_final_message()
print()
print(final.usage)
```

If the model host fails during a stream, or refuses a request after the stream has
started, the stream ends with an `error` event in Anthropic's format instead of
`message_stop`. The HTTP status of a streamed reply is 200 once the stream has
begun. Errors found before the stream starts (authentication, ownership, limits,
invalid requests) are returned with their HTTP status.

## Use tools

```python
tools = [{
    "name": "get_weather",
    "description": "Get the current weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}]
messages = [{"role": "user", "content": "What is the weather in Paris?"}]
response = client.messages.create(
    model="launch-model", max_tokens=256, tools=tools, messages=messages
)
if response.stop_reason == "tool_use":
    messages.append({"role": "assistant", "content": response.content})
    results = [
        {"type": "tool_result", "tool_use_id": block.id, "content": "18°C and clear"}
        for block in response.content
        if block.type == "tool_use"
    ]
    messages.append({"role": "user", "content": results})
    response = client.messages.create(
        model="launch-model", max_tokens=256, tools=tools, messages=messages
    )
print(response.content[-1].text)
```

Function tools need a model host started with automatic tool choice and a tool
parser that matches the model. Your application runs the tools; Fabric does not.

## Supported fields

| Messages field | Sent to the host as |
|---|---|
| `model` | the deployment's served model name |
| `max_tokens` (required, positive integer) | `max_tokens` |
| `system` (string or text blocks) | a leading `system` message; blocks are joined with a blank line |
| `messages` | chat messages, see below |
| `stop_sequences` | `stop` |
| `temperature`, `top_p`, `top_k` | unchanged |
| `stream` | `stream`, with `stream_options.include_usage` so usage can be reported |
| `tools` (custom tools) | function tools (`input_schema` becomes `parameters`) |
| `tool_choice` `auto` / `any` / `tool` / `none` | `"auto"` / `"required"` / a named function / `"none"` |
| `tool_choice.disable_parallel_tool_use: true` | `parallel_tool_calls: false` |
| `thinking` `enabled` or `adaptive` / `disabled` | `chat_template_kwargs.enable_thinking: true` / `false` |
| `routing` (Fabric extension) | consumed by the router, as on chat completions |
| `metadata`, `service_tier`, `cache_control` | accepted and ignored |

`thinking` works with models whose chat template reads `enable_thinking`;
`budget_tokens` is accepted and not enforced. Thinking appears in replies only
when the host runs a reasoning parser.

Content blocks:

- `text` and `image` (`base64` or `url` source) in user turns. Images need a
  vision-capable deployment; `model: "auto"` picks one.
- `tool_use` in assistant turns becomes `tool_calls`; `tool_result` in user turns
  becomes `tool` messages, placed before the rest of that turn. A result with
  `is_error: true` is sent with the prefix `[tool error] `.
- `thinking` and `redacted_thinking` blocks from earlier assistant turns are
  dropped, as chat templates drop earlier reasoning.
- A trailing assistant turn is a prefill: the host continues it
  (`continue_final_message`), and the reply holds only the continuation.

## Replies

- Content is ordered thinking (from the host's reasoning output, with an empty
  `signature`), then text, then `tool_use` blocks.
- If the host produces tool arguments that are not a JSON object, the block's
  `input` is `{}` and the generated text is kept in `fabric_raw_arguments`. Sent
  back in a later request, that text is replayed to the host unchanged.
- `stop_reason`: `end_turn`, `stop_sequence` (with `stop_sequence` set to the
  matched string), `max_tokens`, `tool_use`, or `refusal`.
- `usage.input_tokens` and `usage.output_tokens` are the host's counts. When the
  host reports prefix-cache hits, they are reported as `cache_read_input_tokens`
  and excluded from `input_tokens`, as Anthropic counts them.
- Errors have Anthropic's shape, `{"type": "error", "error": {"type", "message"}}`,
  with the original HTTP status: 400 `invalid_request_error`, 401
  `authentication_error`, 403 `permission_error`, 404 `not_found_error`, 413
  `request_too_large`, 429 `rate_limit_error` (with `Retry-After`), 503
  `overloaded_error`, and other 5xx `api_error`.

## Not supported

These are rejected with HTTP 400 `invalid_request_error`, rather than dropped,
so a model never answers without part of the request:

- `document`, `search_result`, and other block types not listed above; images
  inside `tool_result`; image sources of type `file`.
- Server tools (any tool with a `type` other than `custom`) and a non-empty
  `mcp_servers`.
- Any other top-level field, such as `output_config`, `context_management` or
  `container`.

`POST /v1/messages/count_tokens` is not implemented and returns 404
`not_found_error`. Batches, the Files API, and prompt caching controls are not
available. Request bodies are limited by the same setting as Responses
(`dataPlane.responses.maxBodyBytes`, 16 MiB by default).

See the [Messages API reference](https://docs.anthropic.com/en/api/messages) and
[streaming guide](https://docs.anthropic.com/en/api/messages-streaming)
for the wire format. The lists above describe Fabric's gateway, not every feature
of the upstream API.
