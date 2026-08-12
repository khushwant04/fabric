"""Structured API errors shared by all routers."""

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
        payload: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            payload["error"]["details"] = self.details
        return JSONResponse(status_code=self.status_code, content=payload)


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


class Conflict(ApiError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(409, code, message, details or None)


async def api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render :class:`ApiError` instances as JSON."""
    assert isinstance(exc, ApiError)
    return exc.to_response()
