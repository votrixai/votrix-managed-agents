from copy import deepcopy

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import structlog

from app.governance import QuotaExceededError
from app.governance_runtime import quota_headers


logger = structlog.get_logger(__name__)


def error_payload(
    error_type: str,
    message: str,
    *,
    code: str | None = None,
    request_id: str | None = None,
) -> dict:
    payload = {
        "type": "error",
        "error": {
            "type": error_type,
            "code": code or error_type,
            "message": message,
        },
    }
    if request_id:
        payload["request_id"] = request_id
        payload["error"]["request_id"] = request_id
    return payload


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.error(
            "unhandled_request_error",
            request_id=_request_id(request),
            error_type=exc.__class__.__name__,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=500,
            content=error_payload(
                "api_error",
                "An unexpected error occurred",
                code="internal_error",
                request_id=_request_id(request),
            ),
        )

    @app.exception_handler(QuotaExceededError)
    async def quota_exception_handler(request: Request, exc: QuotaExceededError):
        decision = exc.decision
        code = {
            "active_work": "active_work_quota_exceeded",
            "model_tokens": "model_token_quota_exceeded",
            "storage_bytes": "storage_quota_exceeded",
        }.get(decision.metric, "organization_quota_exceeded")
        return JSONResponse(
            status_code=429,
            content=error_payload(
                "rate_limit_error",
                f"Organization quota exceeded for {decision.metric}",
                code=code,
                request_id=_request_id(request),
            ),
            headers=quota_headers(decision),
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and detail.get("type") == "error":
            payload = _normalized_error_payload(request, detail, exc.status_code)
            return JSONResponse(status_code=exc.status_code, content=payload, headers=exc.headers)
        message = detail if isinstance(detail, str) else "Request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(
                _map_status(exc.status_code),
                message,
                code=_map_code(exc.status_code),
                request_id=_request_id(request),
            ),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=error_payload(
                "invalid_request_error",
                str(exc),
                code="validation_failed",
                request_id=_request_id(request),
            ),
        )


def _normalized_error_payload(request: Request, payload: dict, status_code: int) -> dict:
    normalized = deepcopy(payload)
    error = normalized.setdefault("error", {})
    if isinstance(error, dict):
        error.setdefault("code", _map_code(status_code))
        if request_id := _request_id(request):
            error.setdefault("request_id", request_id)
            normalized.setdefault("request_id", request_id)
    return normalized


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return str(value) if value else None


def _map_status(status_code: int) -> str:
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_error"
    if status_code == 404:
        return "not_found_error"
    if status_code == 409:
        return "conflict_error"
    if status_code == 429:
        return "rate_limit_error"
    return "invalid_request_error"


def _map_code(status_code: int) -> str:
    return {
        400: "invalid_request",
        401: "authentication_failed",
        403: "permission_denied",
        404: "resource_not_found",
        409: "resource_conflict",
        422: "validation_failed",
        429: "rate_limit_exceeded",
    }.get(status_code, "request_failed")
