"""Prompt-prefix block hashes for cache-aware routing (ADR 0019).

A model host reuses the KV cache of a prompt prefix it has already computed. The gateway
cannot see that cache and does not tokenize, so it approximates it: the prompt is
serialized in a canonical, prefix-preserving form, cut into fixed-size blocks of
characters, and each block is identified by a hash chained over every block before it.
Two prompts share their first ``n`` block hashes exactly when they share their first
``n`` blocks of text, which is what a later conversation turn, or a request reusing a
system prompt, does.

Serialization is prefix-preserving: each message is encoded on its own and the encodings
are concatenated in order, so appending a message never changes the encoding of the ones
before it. Only whole blocks are hashed; a trailing partial block is left out, as an
engine's block cache leaves out a partial page.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Characters per block. About 64 tokens of English text, the order of an engine's
#: KV-cache page, so a match of one block is a match worth routing for.
BLOCK_CHARS = 256
#: Bound the work per request: a prompt longer than this many blocks is matched on its
#: first ``MAX_BLOCKS`` only, which is where shared prefixes are.
MAX_BLOCKS = 512

_FIELD = "\x1f"
_RECORD = "\x1e"


def _encode(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _message(message: Any) -> str:
    if not isinstance(message, dict):
        return _encode(message) + _RECORD
    role = message.get("role", "")
    content = message.get("content", "")
    # Tool calls and names are part of what the engine renders for the message.
    extra = {
        key: message[key]
        for key in ("name", "tool_calls", "tool_call_id")
        if key in message
    }
    parts = [_encode(role), _encode(content)]
    if extra:
        parts.append(_encode(extra))
    return _FIELD.join(parts) + _RECORD


def prompt_text(payload: dict[str, Any]) -> str:
    """The parts of a request that become the start of the model's prompt, in order.

    Covers chat completions (``messages``), completions (``prompt``) and responses
    (``instructions`` then ``input``). Tool definitions come first because chat templates
    render them before the conversation. Sampling parameters are left out: they change
    the output, not the prompt.
    """
    pieces: list[str] = []
    tools = payload.get("tools")
    if tools:
        pieces.append("tools" + _FIELD + _encode(tools) + _RECORD)
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions:
        pieces.append("instructions" + _FIELD + instructions + _RECORD)
    for field in ("messages", "input"):
        value = payload.get(field)
        if isinstance(value, list):
            pieces.extend(_message(item) for item in value)
        elif isinstance(value, str):
            pieces.append(_message({"role": "user", "content": value}))
    prompt = payload.get("prompt")
    if isinstance(prompt, str):
        pieces.append(prompt)
    elif isinstance(prompt, list) and len(prompt) == 1 and isinstance(prompt[0], str):
        pieces.append(prompt[0])
    return "".join(pieces)


def prefix_blocks(
    payload: dict[str, Any],
    *,
    block_chars: int = BLOCK_CHARS,
    max_blocks: int = MAX_BLOCKS,
) -> tuple[bytes, ...]:
    """Chained hashes of the prompt's whole blocks, first block first."""
    data = prompt_text(payload).encode("utf-8", "surrogatepass")
    # Blocks are cut on encoded bytes so the cut is the same however the text was built.
    size = max(1, block_chars)
    count = min(len(data) // size, max_blocks)
    hashes: list[bytes] = []
    previous = b""
    for index in range(count):
        digest = hashlib.blake2b(previous, digest_size=16)
        digest.update(data[index * size : (index + 1) * size])
        previous = digest.digest()
        hashes.append(previous)
    return tuple(hashes)
