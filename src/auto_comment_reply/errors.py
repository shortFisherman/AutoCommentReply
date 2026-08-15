"""Typed, user-facing failures raised by the Bilibili adapter."""

from __future__ import annotations

from typing import Any


class BilibiliError(RuntimeError):
    """Base class for a diagnosable Bilibili read failure."""

    category = "bilibili_error"

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        api_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.api_code = api_code
        self.details = details or {}


class NetworkError(BilibiliError):
    category = "network"


class HttpError(BilibiliError):
    category = "http"


class AccessDeniedError(BilibiliError):
    category = "access_denied"


class AuthenticationError(BilibiliError):
    category = "authentication"


class RateLimitError(BilibiliError):
    category = "rate_limit"


class ParameterError(BilibiliError):
    category = "parameter"


class BusinessError(BilibiliError):
    category = "business"


class ResponseParseError(BilibiliError):
    category = "response_parse"


class PaginationError(BilibiliError):
    category = "pagination"
