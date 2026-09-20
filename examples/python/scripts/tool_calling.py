"""Run a local function requested through model tool calling."""

import argparse
import json

from openai.types.chat import ChatCompletionMessageParam

from fabric_examples import Config, fresh_openai_client
from fabric_examples.cli import THINKING_DISABLED, run


def lookup_temperature(city: str) -> dict[str, object]:
    """Deterministic local stand-in; no external service is called."""
    return {"city": city, "temperature_c": 21, "source": "example data"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("city", nargs="?", default="Example City")
    args = parser.parse_args()
    config = Config.from_env()
    messages: list[ChatCompletionMessageParam] = [
        {"role": "user", "content": f"What is the temperature in {args.city}? Use the tool."}
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup_temperature",
                "description": "Get example temperature data for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    with fresh_openai_client(config) as client:
        first = client.chat.completions.create(
            model=config.model, messages=messages, tools=tools, extra_body=THINKING_DISABLED
        )
        message = first.choices[0].message
        messages.append(message)
        if not message.tool_calls:
            raise ValueError("model returned no tool call; tool support is model-dependent")
        for call in message.tool_calls:
            if call.function.name != "lookup_temperature":
                raise ValueError(f"model requested unknown tool {call.function.name!r}")
            arguments = json.loads(call.function.arguments)
            city = arguments.get("city")
            if not isinstance(city, str):
                raise ValueError("tool call did not provide a string city")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(lookup_temperature(city)),
                }
            )
        final = client.chat.completions.create(
            model=config.model, messages=messages, tools=tools, extra_body=THINKING_DISABLED
        )
    print(final.choices[0].message.content or "")


if __name__ == "__main__":
    run(main)
