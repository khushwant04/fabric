"""Bound concurrency and retry transient 429 responses with server guidance."""

import argparse
import asyncio
import random

import openai
from openai import AsyncOpenAI

from fabric_examples import Config, fresh_async_openai_client
from fabric_examples.cli import THINKING_DISABLED, run


async def request_with_retry(
    client: AsyncOpenAI, model: str, prompt: str, semaphore: asyncio.Semaphore
) -> str:
    async with semaphore:
        for attempt in range(5):
            try:
                result = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=64,
                    extra_body=THINKING_DISABLED,
                )
                return result.choices[0].message.content or ""
            except openai.RateLimitError as exc:
                if attempt == 4:
                    raise
                retry_after = exc.response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else min(8.0, 2**attempt)
                await asyncio.sleep(delay + random.uniform(0, 0.25))
    raise RuntimeError("retry loop ended unexpectedly")


async def async_main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1:
        raise ValueError("--requests and --concurrency must be positive")
    config = Config.from_env()
    client = await fresh_async_openai_client(config)
    semaphore = asyncio.Semaphore(args.concurrency)
    async with client:
        results = await asyncio.gather(
            *(
                request_with_retry(
                    client, config.model, f"Reply with request number {index}.", semaphore
                )
                for index in range(args.requests)
            )
        )
    for index, result in enumerate(results):
        print(f"{index}: {result}")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    run(main)
