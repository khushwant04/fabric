"""List model aliases visible to the authenticated account."""

import argparse

from fabric_examples import Config, fresh_openai_client
from fabric_examples.cli import run


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    with fresh_openai_client(Config.from_env()) as client:
        for model in client.models.list().data:
            print(model.id)


if __name__ == "__main__":
    run(main)
