"""Structured API errors, matching the control plane's envelope."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """Application error rendered as a stable JSON envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}

    def to_response(self) -> JSONResponse:
        headers: dict[str, str] | None = None
        retry_after = getattr(self, "retry_after", None)
        if retry_after is not None:
            # A limit a caller can respect needs to say when to come back.
            headers = {"Retry-After": str(retry_after)}
        payload: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            payload["error"]["details"] = self.details
        return JSONResponse(status_code=self.status_code, content=payload, headers=headers)


class BadRequest(ApiError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(400, code, message, details or None)


class Unauthorized(ApiError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(401, code, message, details or None)


class Forbidden(ApiError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(403, code, message, details or None)


class NotFound(ApiError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(404, code, message, details or None)


class TooManyRequests(ApiError):
    """The caller exceeded its share of this stamp.

    Carries ``retry_after`` so a client is told when to return instead of guessing,
    which is the difference between a limit a caller can respect and one that just
    looks like failure.
    """

    def __init__(self, code: str, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(429, code, message)
        self.retry_after = retry_after


class UpstreamUnavailable(ApiError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(503, code, message, details or None)


async def api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return exc.to_response()
