"""Call chat completions directly with httpx rather than an SDK."""

import argparse

import httpx

from fabric_examples import Config, exchange_token
from fabric_examples.auth import USER_AGENT, response_json
from fabric_examples.cli import print_json, run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default="Give one practical use for a compass.")
    args = parser.parse_args()
    config = Config.from_env()
    token = exchange_token(config, "fabric-inference")
    response = httpx.post(
        f"{config.inference_api_url}/chat/completions",
        headers={"Authorization": f"Bearer {token.access_token}", "User-Agent": USER_AGENT},
        json={
            "model": config.model,
            "messages": [{"role": "user", "content": args.prompt}],
            "max_tokens": 128,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=60.0,
    )
    print_json(response_json(response))


if __name__ == "__main__":
    run(main)
