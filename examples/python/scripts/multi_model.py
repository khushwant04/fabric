"""Send the same prompt concurrently to configured or discovered models."""

import argparse
import asyncio

from openai import AsyncOpenAI

from fabric_examples import Config, fresh_async_openai_client
from fabric_examples.cli import THINKING_DISABLED, run


async def ask(client: AsyncOpenAI, model: str, prompt: str) -> tuple[str, str]:
    result = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=128,
        extra_body=THINKING_DISABLED,
    )
    return model, result.choices[0].message.content or ""


async def async_main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default="What makes a good API example?")
    args = parser.parse_args()
    config = Config.from_env()
    client = await fresh_async_openai_client(config)
    async with client:
        models = config.models or tuple(model.id for model in (await client.models.list()).data)
        if not models:
            raise ValueError("no models were configured or discovered")
        results = await asyncio.gather(*(ask(client, model, args.prompt) for model in models))
    for model, answer in results:
        print(f"\n## {model}\n{answer}")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    run(main)
