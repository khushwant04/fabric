"""Model cascade: ``model: "cascade"`` tries the smallest deployment first.

Three chat completions, all with ``model: "cascade"``:

1. An easy question: the small tier's answer passes the checks, so it stays there.
2. A long step-by-step question: the small tier runs out of tokens (``finish_reason: length``)
   and the gateway escalates to the next tier.
3. A structured request (``response_format: json_schema``): the small tier's JSON misses a
   required field, so the gateway escalates.

The gateway reports the path in ``X-Fabric-Cascade-Path`` / ``-Escalations`` / ``-Reason`` and
the body's ``model`` is the tier that produced the answer. Every attempt is metered.

Configure with the usual example variables (FABRIC_CONTROL_URL, FABRIC_INFERENCE_URL,
FABRIC_API_KEY). The account needs two or more deployments ordered by ``cascade_tier`` or
``capabilities.priority``. `make demo-cascade` runs it against a local platform with two
stub deployments, no GPU needed.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

from fabric_examples import Config
from fabric_examples.auth import USER_AGENT, exchange_token, raise_for_fabric_status
from fabric_examples.cli import THINKING_DISABLED, run

_COLOR = sys.stdout.isatty() and not os.getenv("NO_COLOR") or bool(os.getenv("FORCE_COLOR"))


def _style(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(text: str) -> str:
    return _style("1", text)


def dim(text: str) -> str:
    return _style("2", text)


def green(text: str) -> str:
    return _style("32", text)


def yellow(text: str) -> str:
    return _style("33", text)


def banner(title: str) -> None:
    print()
    print(bold(f"━━ {title} " + "━" * max(0, 68 - len(title))))


CITY_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "country": {"type": "string"},
        "population_millions": {"type": "number"},
        "landmarks": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["city", "country", "population_millions", "landmarks"],
}

PROMPTS: list[tuple[str, dict[str, Any]]] = [
    (
        "Easy question",
        {"messages": [{"role": "user", "content": "What is the capital of Portugal?"}]},
    ),
    (
        "Long reasoning",
        {
            "messages": [
                {
                    "role": "user",
                    "content": "A train covers 120 km in 1.5 hours, then 200 km more at the "
                    "same speed. Explain step by step how long the whole journey takes.",
                }
            ],
            "max_tokens": 64,
        },
    ),
    (
        "Structured output",
        {
            "messages": [{"role": "user", "content": "Describe Lisbon as JSON."}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "city", "schema": CITY_SCHEMA},
            },
        },
    ),
]


def main() -> None:
    config = Config.from_env()
    token = exchange_token(config, "fabric-inference")
    headers = {"Authorization": f"Bearer {token.access_token}", "User-Agent": USER_AGENT}
    with httpx.Client(base_url=config.inference_api_url, headers=headers, timeout=120) as client:
        models = client.get("/models")
        raise_for_fabric_status(models)
        print(dim(f"  deployments on this account: {[m['id'] for m in models.json()['data']]}"))
        for index, (title, body) in enumerate(PROMPTS, start=1):
            banner(f"{index}. {title}  model=cascade")
            print(dim(f"  prompt: {body['messages'][-1]['content']}"))
            response = client.post(
                "/chat/completions", json={"model": "cascade", **body, **THINKING_DISABLED}
            )
            raise_for_fabric_status(response)
            h = response.headers
            reply = response.json()
            escalations = int(h.get("x-fabric-cascade-escalations", "0"))
            path = h.get("x-fabric-cascade-path", "?").replace(">", " → ")
            print(f"  path        {bold(path)}")
            reason = h.get("x-fabric-cascade-reason", "?")
            print(
                f"  escalations {escalations}  reason "
                f"{(yellow if escalations else green)(reason)}"
            )
            print(f"  answered by {bold(reply['model'])}")
            content = reply["choices"][0]["message"].get("content") or ""
            if body.get("response_format"):
                content = json.dumps(json.loads(content))
            print(f"  {green('✓')} {content[:160]}")
    print()


if __name__ == "__main__":
    run(main)
