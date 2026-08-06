"""Assembling the FastAPI application."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings

from app.models.errors import (
    Conflict,
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
    billing_accounts,
    environments,
    files,
    health,
    internal_work,
    llm,
    memory,
    sessions,
    skills,
)

ROUTERS = (
    health.router,
    accounts.router,
    billing_accounts.router,
    agents.router,
    sessions.router,
    environments.router,
    files.router,
    skills.router,
    memory.router,
    llm.router,
    internal_work.router,
)


def create_app() -> FastAPI:
    app = FastAPI(title="Votrix Managed Agents", version="0.1.0")
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
# a VMA API key. What a trusted origin puts in its own request headers was
# never the thing CORS was protecting.
CORS_REQUEST_HEADERS = ("*",)
# Correlation ids are attached by the edge router. Listing them here is what
# lets page JavaScript read the id it needs to quote in a support request.
CORS_EXPOSED_HEADERS = ("request-id", "x-request-id")
CORS_PREFLIGHT_MAX_AGE_SECONDS = 600


def _install_cors(app: FastAPI) -> None:
    """Allow the configured browser origins, and no others.

    Credentials stay off: VMA authenticates with the `x-api-key` header rather
    than cookies, so no response ever needs to be readable by a page that did
    not already hold the key. Keeping it off also removes the wildcard-origin
    footgun, since the browser refuses `*` alongside credentials.
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
