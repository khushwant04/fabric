"""Environment-backed configuration shared by the examples."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DEFAULT_MODEL = "qwen3.5-0.8b"


class ConfigError(ValueError):
    """Raised when example configuration is missing or invalid."""


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required; see examples/.env.example")
    return value


def _normalize_service_url(name: str, value: str) -> str:
    raw = value.strip().rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ConfigError(f"{name} must be an absolute http(s) URL")
    if parts.query or parts.fragment:
        raise ConfigError(f"{name} must not contain a query string or fragment")
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parts.scheme, parts.netloc, path.rstrip("/"), "", ""))


def _optional_path(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    return Path(value).expanduser() if value else None


@dataclass(frozen=True, slots=True)
class Config:
    """Validated Fabric example settings.

    URLs are stored without a trailing slash or ``/v1``. Use the URL properties when
    constructing requests.
    """

    control_url: str
    inference_url: str
    api_key: str
    model: str = DEFAULT_MODEL
    models: tuple[str, ...] = ()
    image_url: str | None = None
    image_file: Path | None = None
    audio_file: Path | None = None

    @classmethod
    def from_env(cls) -> Config:
        configured_models = tuple(
            model.strip() for model in os.getenv("FABRIC_MODELS", "").split(",") if model.strip()
        )
        return cls(
            control_url=_normalize_service_url(
                "FABRIC_CONTROL_URL", _required_env("FABRIC_CONTROL_URL")
            ),
            inference_url=_normalize_service_url(
                "FABRIC_INFERENCE_URL", _required_env("FABRIC_INFERENCE_URL")
            ),
            api_key=_required_env("FABRIC_API_KEY"),
            model=os.getenv("FABRIC_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            models=configured_models,
            image_url=os.getenv("FABRIC_IMAGE_URL", "").strip() or None,
            image_file=_optional_path("FABRIC_IMAGE_FILE"),
            audio_file=_optional_path("FABRIC_AUDIO_FILE"),
        )

    @property
    def token_url(self) -> str:
        return f"{self.control_url}/v1/token"

    @property
    def control_api_url(self) -> str:
        return f"{self.control_url}/v1"

    @property
    def inference_api_url(self) -> str:
        return f"{self.inference_url}/v1"
