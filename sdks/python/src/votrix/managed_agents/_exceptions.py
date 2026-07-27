from __future__ import annotations

from typing import Any

import httpx


class VotrixError(Exception):
    """Base class for SDK errors. Secrets and request bodies are never rendered."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self)!r})"


class APIConnectionError(VotrixError):
    def __init__(self, message: str, *, request: httpx.Request | None = None) -> None:
        super().__init__(message)
        self.request = request


class APITimeoutError(APIConnectionError):
    pass


class APIResponseValidationError(VotrixError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        request_id: str | None = None,
        headers: httpx.Headers | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.headers = headers or httpx.Headers()
        self.rate_limit_headers = _rate_limit_headers(self.headers)


class APIStatusError(VotrixError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        error_type: str | None,
        request_id: str | None,
        body: Any,
        response: httpx.Response,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.error_code = error_code
        self.request_id = request_id
        self.body = body
        self.response = response
        self.headers = response.headers
        self.retry_after = response.headers.get("retry-after")
        self.rate_limit_headers = _rate_limit_headers(response.headers)


class BadRequestError(APIStatusError):
    pass


class AuthenticationError(APIStatusError):
    pass


class PermissionDeniedError(APIStatusError):
    pass


class NotFoundError(APIStatusError):
    pass


class ConflictError(APIStatusError):
    pass


class UnprocessableEntityError(APIStatusError):
    pass


class RateLimitError(APIStatusError):
    pass


class InternalServerError(APIStatusError):
    pass


class APIStreamError(VotrixError):
    def __init__(self, message: str, *, error_type: str | None = None, request_id: str | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.request_id = request_id


STATUS_ERRORS: dict[int, type[APIStatusError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: UnprocessableEntityError,
    429: RateLimitError,
}


def _rate_limit_headers(headers: httpx.Headers) -> dict[str, str]:
    """Expose vendor-neutral and vendor-prefixed rate-limit metadata."""

    return {
        name: value
        for name, value in headers.items()
        if "ratelimit" in name.lower() or "rate-limit" in name.lower() or name.lower() == "retry-after"
    }
