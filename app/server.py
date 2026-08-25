"""Assembling the FastAPI application."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

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
    ProviderRateLimited,
    SandboxUnavailable,
    SessionBusy,
    UsageUnavailable,
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
    sandbox,
    sessions,
    skills,
)

ROUTERS = (
    health.router,
    organization_api_keys.router,
    accounts.router,
    agents.router,
    sessions.router,
    environments.router,
    sandbox.router,
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

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": {"type": "not_found", "message": str(exc)}})

    @app.exception_handler(UsageUnavailable)
    async def _usage_unavailable(
        request: Request, exc: UsageUnavailable
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "type": "usage_unavailable",
                    "message": str(exc),
                }
            },
        )

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

    @app.exception_handler(ProviderRateLimited)
    async def _provider_rate_limited(
        request: Request, exc: ProviderRateLimited
    ) -> JSONResponse:
        # No `Retry-After`: what frees a slot is somebody else's container
        # finishing, and this service has no idea when that is. A guessed
        # number would send every waiting client back at the same wrong moment.
        return JSONResponse(
            status_code=429,
            content={"error": {"type": "rate_limit_error", "message": str(exc)}},
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
