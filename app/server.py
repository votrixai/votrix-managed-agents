"""Assembling the FastAPI application."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings

from app.models.errors import (
    Conflict,
    CredentialValidationUnavailable,
    InvalidRequest,
    MemoryPreconditionFailed,
    MemoryStoreUnavailable,
    NotFound,
    PayloadTooLarge,
    SandboxUnavailable,
    SessionBusy,
)
from app.routers import (
    accounts,
    agents,
    environments,
    files,
    health,
    internal_work,
    llm,
    memory,
    organization_api_keys,
    organizations,
    sessions,
    skills,
)

ROUTERS = (
    health.router,
    organization_api_keys.router,
    organizations.router,
    accounts.router,
    agents.router,
    sessions.router,
    environments.router,
    files.router,
    skills.router,
    memory.router,
    llm.router,
    internal_work.router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    sweeper = _start_sweeper()
    try:
        yield
    finally:
        if sweeper is not None:
            sweeper.cancel()

    # The checkpoint pools outlive any one turn, so nothing else would ever
    # close them. Imported here rather than at module scope: `app.runtime` pulls
    # in LangGraph and the model clients, and the API process should not pay
    # that import unless it actually runs a turn.
    from app.runtime.engine import aclose_checkpoint_pools

    await aclose_checkpoint_pools()


def _start_sweeper() -> asyncio.Task | None:
    """Put the janitor on this process's event loop, if it is the one for it.

    A turn dropped by a dying instance leaves its session `running` forever —
    the lease lapses, so the next message can still claim it, but until someone
    sends one the conversation reads as though the agent is still typing. The
    sweep is what closes those out without a user having to bump into one.

    Held in a variable because asyncio keeps only a weak reference to a running
    task: without a strong one the loop could be collected mid-sleep.
    """
    if not get_settings().vma_run_sweeper:
        return None

    from app.worker import run_forever

    return asyncio.create_task(run_forever())


def create_app() -> FastAPI:
    app = FastAPI(title="Votrix Managed Agents", version="0.1.0", lifespan=lifespan)
    for router in ROUTERS:
        app.include_router(router)
    _install_cors(app)
    _install_error_handlers(app)
    return app


# Any request header, from an origin already on the list above.
#
# Enumerating them was a losing game. A preflight naming one header this list
# omits is rejected outright, taking every endpoint down at once — and the
# browser attaches headers the caller never wrote: `fetch(url, {cache:
# "no-cache"})` alone adds `Cache-Control` and `Pragma`, neither of them
# CORS-safelisted. Each name added here only revealed the next one.
#
# This is not a loosened boundary. The origin list is unchanged and still
# explicit, credentials are still off, and every request still has to present
# either an Organization API key or a verified first-party user token plus a
# membership-scoped Organization. What a trusted origin puts in its own request
# headers was never the thing CORS was protecting.
CORS_REQUEST_HEADERS = ("*",)
# Correlation ids are attached by the edge router. Listing them here is what
# lets page JavaScript read the id it needs to quote in a support request.
CORS_EXPOSED_HEADERS = ("request-id", "x-request-id")
CORS_PREFLIGHT_MAX_AGE_SECONDS = 600

_REDACTED_INPUT = "**********"
_SENSITIVE_REQUEST_FIELDS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)


def _install_cors(app: FastAPI) -> None:
    """Allow the configured browser origins, and no others.

    Credentials stay off: VMA authenticates with explicit request headers, not
    cookies. API consumers send `x-api-key`; the first-party Console BFF sends
    a Supabase bearer token and an Organization that VMA verifies against the
    user's membership. Keeping browser credentials off also removes the
    wildcard-origin footgun, since browsers refuse `*` alongside credentials.
    """
    origins = get_settings().cors_origins
    if not origins:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=list(CORS_REQUEST_HEADERS),
        expose_headers=list(CORS_EXPOSED_HEADERS),
        max_age=CORS_PREFLIGHT_MAX_AGE_SECONDS,
    )


def _install_error_handlers(app: FastAPI) -> None:
    """Map service failures onto status codes.

    Doing it here is what lets `app/services` stay unaware of HTTP: a service
    raises what went wrong, and this is the only place that decides what that
    looks like on the wire.
    """

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(_safe_validation_errors(exc))},
        )

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": {"type": "not_found", "message": str(exc)}})

    @app.exception_handler(Conflict)
    async def _conflict(request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": {"type": "conflict", "message": str(exc)}})

    @app.exception_handler(MemoryPreconditionFailed)
    async def _memory_precondition_failed(
        request: Request, exc: MemoryPreconditionFailed
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "type": "memory_precondition_failed_error",
                    "message": str(exc),
                }
            },
        )

    @app.exception_handler(InvalidRequest)
    async def _invalid_request(request: Request, exc: InvalidRequest) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {"type": "invalid_request_error", "message": str(exc)}
            },
        )

    @app.exception_handler(PayloadTooLarge)
    async def _payload_too_large(
        request: Request, exc: PayloadTooLarge
    ) -> JSONResponse:
        return JSONResponse(
            status_code=413,
            content={
                "error": {"type": "request_too_large", "message": str(exc)}
            },
        )

    @app.exception_handler(SessionBusy)
    async def _session_busy(request: Request, exc: SessionBusy) -> JSONResponse:
        # No `Retry-After`. How long the agent has left to think is not
        # something this service knows, and a header saying otherwise would
        # send well-behaved clients back at the wrong moment.
        return JSONResponse(
            status_code=409,
            content={"error": {"type": "session_busy", "message": str(exc)}},
        )

    @app.exception_handler(SandboxUnavailable)
    async def _sandbox_unavailable(request: Request, exc: SandboxUnavailable) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"error": {"type": "sandbox_unavailable", "message": str(exc)}},
        )

    @app.exception_handler(MemoryStoreUnavailable)
    async def _memory_store_unavailable(
        request: Request, exc: MemoryStoreUnavailable
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {"type": "memory_store_unavailable", "message": str(exc)}
            },
        )

    @app.exception_handler(CredentialValidationUnavailable)
    async def _credential_validation_unavailable(
        request: Request, exc: CredentialValidationUnavailable
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "type": "credential_validation_unavailable",
                    "message": str(exc),
                }
            },
        )


def _safe_validation_errors(exc: RequestValidationError) -> list[dict]:
    """Keep FastAPI's validation shape without echoing request secrets.

    Pydantic includes the rejected input in each error. For a discriminated
    union or a model-level validator that input can be the whole object, which
    means a malformed BYOK request would otherwise return its API key in the
    422 body. Preserve useful field values while recursively masking known
    secret fields.
    """

    errors: list[dict] = []
    for error in exc.errors():
        safe = dict(error)
        location = {str(part) for part in safe.get("loc", ())}
        if "input" in safe:
            safe["input"] = (
                _REDACTED_INPUT
                if any(_is_sensitive_request_field(part) for part in location)
                else _redact_sensitive_input(safe["input"])
            )
        errors.append(safe)
    return errors


def _redact_sensitive_input(value: object) -> object:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            redacted[key] = (
                _REDACTED_INPUT
                if _is_sensitive_request_field(str(key))
                else _redact_sensitive_input(item)
            )
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive_input(item) for item in value]
    return value


def _is_sensitive_request_field(value: str) -> bool:
    normalized = value.lower().replace("-", "_")
    return normalized in _SENSITIVE_REQUEST_FIELDS or normalized.endswith(
        ("_api_key", "_password", "_secret", "_token")
    )
