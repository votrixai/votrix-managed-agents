"""Assembling the FastAPI application."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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
    _install_error_handlers(app)
    return app


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
