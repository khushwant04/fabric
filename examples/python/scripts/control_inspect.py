"""Inspect read-only control-plane identity, account, deployment, status, and usage data."""

import argparse
from typing import Any

import httpx

from fabric_examples import Config, exchange_token
from fabric_examples.auth import bearer_headers, response_json
from fabric_examples.cli import print_json, run


def get(client: httpx.Client, path: str) -> dict[str, Any] | list[Any]:
    return response_json(client.get(path))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deployment", help="Inspect one deployment ID; defaults to every deployment"
    )
    args = parser.parse_args()
    config = Config.from_env()
    token = exchange_token(config, "fabric-control")
    account_path = f"accounts/{token.account_id}"
    with httpx.Client(
        base_url=config.control_api_url,
        headers=bearer_headers(token),
        timeout=30.0,
    ) as client:
        output: dict[str, Any] = {
            "self": get(client, "self"),
            "account": get(client, account_path),
        }
        deployments = get(client, f"{account_path}/deployments")
        output["deployments"] = deployments
        ids = (
            [args.deployment]
            if args.deployment
            else [
                str(item["id"]) for item in deployments if isinstance(item, dict) and "id" in item
            ]
            if isinstance(deployments, list)
            else []
        )
        output["deployment_details"] = {
            deployment_id: {
                "deployment": get(client, f"{account_path}/deployments/{deployment_id}"),
                "status": get(client, f"{account_path}/deployments/{deployment_id}/status"),
                "usage": get(client, f"{account_path}/deployments/{deployment_id}/usage"),
            }
            for deployment_id in ids
        }
    print_json(output)


if __name__ == "__main__":
    run(main)
