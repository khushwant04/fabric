"""Call the legacy text completions endpoint."""

import argparse

from fabric_examples import Config, fresh_openai_client
from fabric_examples.cli import run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default="A short definition of gravity is")
    args = parser.parse_args()
    config = Config.from_env()
    with fresh_openai_client(config) as client:
        result = client.completions.create(model=config.model, prompt=args.prompt, max_tokens=128)
    print(result.choices[0].text)


if __name__ == "__main__":
    run(main)
