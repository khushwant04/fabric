# Fabric API examples

Runnable Python and curl examples for Fabric's control and OpenAI-compatible inference APIs. They exchange a long-lived Fabric API key for a short-lived, audience-specific JWT; the raw key is never sent to inference.

## Quickstart

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required. From the repository root:

```bash
cp examples/.env.example examples/.env
# Edit examples/.env with your service URLs and API key, then export it:
set -a; source examples/.env; set +a
uv sync --project examples/python --extra dev
uv run --project examples/python python examples/python/scripts/list_models.py
uv run --project examples/python python examples/python/scripts/chat.py "Why is the sky blue?"
```

`FABRIC_CONTROL_URL`, `FABRIC_INFERENCE_URL`, and `FABRIC_API_KEY` are required. `FABRIC_MODEL` defaults to `qwen3.5-0.8b`. Optional inputs are `FABRIC_MODELS` (comma-separated aliases), `FABRIC_IMAGE_URL`, `FABRIC_IMAGE_FILE`, and `FABRIC_AUDIO_FILE`. URLs may include `/v1`; helpers normalize it.

## Command catalog

Run every Python command from the repository root using this form:

```bash
uv run --project examples/python python examples/python/scripts/<name>.py --help
```

| Script | Purpose |
|---|---|
| `list_models.py` | List model aliases visible to the account. |
| `chat.py [prompt]` | Non-streaming chat completion. |
| `stream_chat.py [prompt]` | Stream chat text deltas. |
| `completion.py [prompt]` | Legacy text completion. |
| `vision.py [--image-url URL \| --image-file PATH]` | Remote-URL or local-data-URL vision input. |
| `multi_model.py [prompt]` | Concurrently send one prompt to `FABRIC_MODELS`, or all discovered models. |
| `audio.py transcribe\|translate [--file PATH]` | Non-streaming audio transcription or English translation. |
| `rate_limits.py [--requests N --concurrency N]` | Bounded concurrency and 429 retry handling. |
| `tool_calling.py [city]` | Manual local function/tool call loop. |
| `control_inspect.py [--deployment ID]` | Read self, account, deployments, per-stamp status, and aggregate usage. |
| `raw_http_chat.py [prompt]` | Chat through raw `httpx`, without the OpenAI SDK. |
| `agents_tool.py [city]` | Experimental OpenAI Agents SDK local function tool. |

The Agents example needs its optional dependency:

```bash
uv sync --project examples/python --extra agents
uv run --project examples/python python examples/python/scripts/agents_tool.py
```

Curl examples require `curl` and `jq`. After exporting the same environment, run (for example) `bash examples/curl/models.sh`, `bash examples/curl/chat.sh`, `bash examples/curl/stream_chat.sh`, `bash examples/curl/completion.sh`, `bash examples/curl/vision.sh`, `bash examples/curl/audio.sh transcriptions`, or `bash examples/curl/control_read.sh [deployment-id]`. They use strict shell mode, safely construct JSON with `jq`, and never display tokens.

## API support matrix

| API | Fabric support | Example |
|---|---|---|
| Models | Supported | `list_models.py`, `models.sh` |
| Chat completions | Supported, including streaming pass-through | `chat.py`, `stream_chat.py` |
| Legacy completions | Supported | `completion.py` |
| Audio transcriptions/translations | Supported route; model availability varies | `audio.py`, `audio.sh` |
| Responses | **Unsupported** (`/v1/responses` is absent) | Do not use |
| Embeddings | Unsupported | — |
| Image generation | Unsupported | — |
| Assistants | Unsupported | — |
| Realtime | Unsupported | — |

## Token lifecycle

`fabric_examples.auth` posts to `<control>/v1/token` with an explicit `User-Agent`, `grant_type=api_key`, and either the `fabric-inference` or `fabric-control` audience. API-key exchange derives the account, so no account ID is requested. Its typed `AccessToken` retains the token type, account ID, scopes, and calculated expiry. Client helpers exchange a fresh inference token and create a fresh sync or async `OpenAI` client at `<inference>/v1`. Control examples use a separate control token. Never log the API key, exchanged token, request headers, or complete client configuration. Re-exchange when a long-running process approaches token expiry.

## Caveats and expected errors

- **Model discovery:** `/models` returns aliases accessible to the account. A configured alias can disappear or be temporarily unavailable; discovery does not guarantee every model supports every feature.
- **Multimodal:** image input is transparently forwarded, but only vision-capable deployments accept it. Local files become data URLs and can be large; use appropriate image sizes and formats.
- **Streaming:** chat streaming uses server-sent events and may expose errors only after headers arrive. The examples print text deltas; other delta fields remain available in the SDK object.
- **Thinking mode:** chat examples pass `chat_template_kwargs.enable_thinking=false` for predictable concise output. This backend-specific option may be ignored or rejected by models that do not implement it; remove `extra_body` if necessary.
- **Audio:** Fabric exposes transcription and translation routes, but a currently compatible audio model may not exist in a given installation. Audio streaming is not supported. File formats, limits, languages, and translation behavior are model-dependent.
- **Manual tools:** Fabric forwards chat tool definitions; the selected model must produce compatible tool calls. Execute only explicitly allowlisted local functions, validate arguments, and never execute model-generated code.
- **Agents:** `agents_tool.py` explicitly uses `AsyncOpenAI`, Fabric's `/v1` base, an inference JWT, and `OpenAIChatCompletionsModel`; tracing is disabled. Compatibility is experimental and model-dependent. It deliberately does not use Responses because Fabric has no `/v1/responses` route.
- **Concurrency and 429:** start with low bounded concurrency. `rate_limits.py` honors `Retry-After` when present and otherwise uses jittered exponential delay. Repeated 429s should be surfaced rather than retried forever.
- **Read-only control APIs:** API-key scopes determine access. Status is per stamp and can be empty or stale. Usage is operational aggregate metering, not billing-grade accounting. The example derives the account from token exchange and performs no mutations.

Typical failures include `401` for invalid/expired tokens or a wrong audience, `403` for missing scopes/account access, `404` for an unknown model or deployment, `413` for oversized audio/image requests, `422` for malformed payloads, `429` for concurrency/rate limits, and `5xx`/connection errors for unavailable services or model hosts. Error helpers report status and safe server detail without intentionally printing credentials.

## Agents SDK sources

The Agents integration follows the official [model configuration documentation](https://openai.github.io/openai-agents-python/models) and [SDK configuration documentation](https://openai.github.io/openai-agents-python/config/). In particular, the explicit chat-completions adapter avoids the SDK's Responses-oriented defaults, and tracing is disabled to prevent external trace export. Web material was rephrased for compliance with licensing restrictions.
