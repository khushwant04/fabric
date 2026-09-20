"""Small CLI utilities shared by example scripts."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

import httpx
import openai

from fabric_examples.auth import FabricAPIError
from fabric_examples.config import ConfigError

THINKING_DISABLED = {"chat_template_kwargs": {"enable_thinking": False}}


def print_json(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def run(main: Callable[[], None]) -> None:
    """Run an example with concise errors and no credential output."""
    try:
        main()
    except (ConfigError, FabricAPIError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except (httpx.HTTPError, openai.APIError) as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
