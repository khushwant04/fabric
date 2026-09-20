"""Typed token exchange and OpenAI client construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import httpx
from openai import AsyncOpenAI, OpenAI

from fabric_examples.config import Config

Audience = Literal["fabric-control", "fabric-inference"]
USER_AGENT = "fabric-api-examples/1.0"


class FabricAPIError(RuntimeError):
    """A safe, credential-free summary of a Fabric HTTP error."""


def _error_message(response: httpx.Response) -> str:
    code = "request_failed"
    message = response.reason_phrase or "Fabric request failed"
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            code = str(error.get("code", code))
            message = str(error.get("message", message))
        elif isinstance(body.get("detail"), str):
            message = body["detail"]
    return f"Fabric API returned HTTP {response.status_code} ({code}): {message}"


def raise_for_fabric_status(response: httpx.Response) -> None:
    if response.is_error:
        raise FabricAPIError(_error_message(response))


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A short-lived, account-bound Fabric access token."""

    access_token: str
    account_id: str
    scopes: tuple[str, ...]
    expires_at: datetime
    token_type: str = "Bearer"

    @property
    def expires_in(self) -> int:
        return max(0, int((self.expires_at - datetime.now(UTC)).total_seconds()))

    def expires_soon(self, *, within_seconds: int = 60) -> bool:
        return self.expires_in <= within_seconds


def _parse_token(payload: Any) -> AccessToken:
    if not isinstance(payload, dict):
        raise FabricAPIError("Token exchange returned an invalid JSON object")
    try:
        access_token = payload["access_token"]
        account_id = payload["account_id"]
        expires_in = int(payload["expires_in"])
        token_type = payload.get("token_type", "Bearer")
        scope = payload.get("scope", "")
    except (KeyError, TypeError, ValueError) as exc:
        raise FabricAPIError("Token exchange response is missing required fields") from exc
    if not isinstance(access_token, str) or not access_token:
        raise FabricAPIError("Token exchange returned an empty access token")
    if not isinstance(account_id, str) or not account_id:
        raise FabricAPIError("Token exchange returned an invalid account ID")
    if expires_in <= 0:
        raise FabricAPIError("Token exchange returned a non-positive expiry")
    if not isinstance(scope, str):
        raise FabricAPIError("Token exchange returned an invalid scope value")
    return AccessToken(
        access_token=access_token,
        account_id=account_id,
        scopes=tuple(scope.split()),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        token_type=str(token_type),
    )


def _token_request(config: Config, audience: Audience) -> dict[str, str]:
    return {"grant_type": "api_key", "api_key": config.api_key, "audience": audience}


def exchange_token(config: Config, audience: Audience) -> AccessToken:
    """Exchange the configured API key for one fresh audience-specific token."""
    try:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20.0) as client:
            response = client.post(config.token_url, json=_token_request(config, audience))
    except httpx.HTTPError as exc:
        raise FabricAPIError(f"Token exchange failed before a response: {exc}") from exc
    raise_for_fabric_status(response)
    return _parse_token(response.json())


async def exchange_token_async(config: Config, audience: Audience) -> AccessToken:
    """Asynchronously exchange the configured API key for one fresh token."""
    try:
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=20.0) as client:
            response = await client.post(config.token_url, json=_token_request(config, audience))
    except httpx.HTTPError as exc:
        raise FabricAPIError(f"Token exchange failed before a response: {exc}") from exc
    raise_for_fabric_status(response)
    return _parse_token(response.json())


def fresh_openai_client(config: Config) -> OpenAI:
    """Create a new sync OpenAI client backed by a fresh inference token."""
    token = exchange_token(config, "fabric-inference")
    return OpenAI(
        api_key=token.access_token,
        base_url=config.inference_api_url,
        default_headers={"User-Agent": USER_AGENT},
    )


async def fresh_async_openai_client(config: Config) -> AsyncOpenAI:
    """Create a new async OpenAI client backed by a fresh inference token."""
    token = await exchange_token_async(config, "fabric-inference")
    return AsyncOpenAI(
        api_key=token.access_token,
        base_url=config.inference_api_url,
        default_headers={"User-Agent": USER_AGENT},
    )


def bearer_headers(token: AccessToken) -> dict[str, str]:
    """Build safe request headers for a short-lived token."""
    return {
        "Authorization": f"{token.token_type} {token.access_token}",
        "User-Agent": USER_AGENT,
    }


def response_json(response: httpx.Response) -> dict[str, Any] | list[Any]:
    """Validate a Fabric response and return its JSON collection."""
    raise_for_fabric_status(response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise FabricAPIError("Fabric API returned a non-JSON response") from exc
    if not isinstance(payload, dict | list):
        raise FabricAPIError("Fabric API returned an unexpected JSON value")
    return cast(dict[str, Any] | list[Any], payload)
