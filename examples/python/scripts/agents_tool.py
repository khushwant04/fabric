"""Experimental Agents SDK example using Fabric chat completions and a local tool."""

import argparse
import asyncio

from agents import Agent, OpenAIChatCompletionsModel, Runner, function_tool, set_tracing_disabled

from fabric_examples import Config, fresh_async_openai_client
from fabric_examples.cli import run


@function_tool
def example_temperature(city: str) -> str:
    """Return deterministic example temperature data for a city."""
    return f"The example temperature for {city} is 21 C."


async def async_main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("city", nargs="?", default="Example City")
    args = parser.parse_args()
    config = Config.from_env()
    set_tracing_disabled(True)
    client = await fresh_async_openai_client(config)
    async with client:
        model = OpenAIChatCompletionsModel(model=config.model, openai_client=client)
        agent = Agent(
            name="Fabric example assistant",
            instructions="Use the local temperature tool, then answer concisely.",
            model=model,
            tools=[example_temperature],
        )
        result = await Runner.run(agent, f"What is the temperature in {args.city}?")
    print(result.final_output)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    run(main)
