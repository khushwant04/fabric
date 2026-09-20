"""Stream a chat completion as text deltas arrive."""

import argparse

from fabric_examples import Config, fresh_openai_client
from fabric_examples.cli import THINKING_DISABLED, run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default="Describe a calm ocean in three sentences.")
    args = parser.parse_args()
    config = Config.from_env()
    with fresh_openai_client(config) as client:
        stream = client.chat.completions.create(
            model=config.model,
            messages=[{"role": "user", "content": args.prompt}],
            max_tokens=256,
            stream=True,
            extra_body=THINKING_DISABLED,
        )
        for chunk in stream:
            print(chunk.choices[0].delta.content or "", end="", flush=True)
    print()


if __name__ == "__main__":
    run(main)
