"""Send one non-streaming chat completion request."""

import argparse

from fabric_examples import Config, fresh_openai_client
from fabric_examples.cli import THINKING_DISABLED, run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default="Explain why leaves change color.")
    args = parser.parse_args()
    config = Config.from_env()
    with fresh_openai_client(config) as client:
        result = client.chat.completions.create(
            model=config.model,
            messages=[{"role": "user", "content": args.prompt}],
            max_tokens=256,
            extra_body=THINKING_DISABLED,
        )
    print(result.choices[0].message.content or "")


if __name__ == "__main__":
    run(main)
