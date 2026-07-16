import asyncio
from contextlib import asynccontextmanager, suppress
import re
from time import perf_counter
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.auth import AuthProvider, default_auth_provider
from app.config import get_settings
from app.errors import error_payload, install_error_handlers
from app.logging import setup as setup_logging
from app.models.status import CapabilityManifestResponse, DatabaseHealthResponse, HealthResponse
from app.public_surface import capability_manifest, is_public_ga_path, public_ga_openapi
from app.routers import (
    agents,
    api_keys,
    environments,
    files,
    generic_resources,
    model_providers,
    sessions,
    skills,
)


logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(
        app_env=settings.app_env,
        sentry_dsn=settings.sentry_dsn,
        log_level=settings.log_level,
    )
    janitor_stop = asyncio.Event()
    janitor_task: asyncio.Task | None = None
    worker_stop = asyncio.Event()
    worker_tasks: list[asyncio.Task] = []
    if str(settings.vma_sandbox_provider).strip().lower() == "e2b" and settings.e2b_api_key:
        from app.runtime.sandbox_lifecycle import run_sandbox_janitor

        janitor_task = asyncio.create_task(
            run_sandbox_janitor(janitor_stop),
            name="vma-e2b-janitor",
        )
    if settings.vma_embedded_worker_enabled:
        from app.worker import run_worker

        for index in range(settings.vma_worker_concurrency):
            worker_id = f"embedded-{index}-{uuid4().hex}"
            worker_tasks.append(
                asyncio.create_task(
                    run_worker(
                        environment_id=None,
                        poll_interval_seconds=settings.vma_worker_poll_interval_seconds,
                        once=False,
                        worker_id=worker_id,
                        lease_seconds=settings.vma_worker_lease_seconds,
                        stop_event=worker_stop,
                    ),
                    name=f"vma-embedded-worker-{index}",
                )
            )
    try:
        yield
    finally:
        janitor_stop.set()
        worker_stop.set()
        if janitor_task is not None:
            try:
                await asyncio.wait_for(janitor_task, timeout=5)
            except TimeoutError:
                janitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await janitor_task
        if worker_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*worker_tasks), timeout=5)
            except TimeoutError:
                for task in worker_tasks:
                    task.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.gather(*worker_tasks)


def create_app(*, auth_provider: AuthProvider | None = None) -> FastAPI:
    # The packaged factory is also the local run.sh entrypoint. Load .env here
    # so dynamically configured provider credentials are available via their
    # api_key_env names; process-level Cloud Run variables still take priority.
    load_dotenv()
    app = FastAPI(
        title="Votrix Managed Agents",
        description=(
            "Self-hosted, multi-tenant control plane for long-running agents."
        ),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.auth_provider = auth_provider or default_auth_provider()
    install_error_handlers(app)

    settings = get_settings()

    @app.middleware("http")
    async def enforce_public_ga_surface(request: Request, call_next):
        if settings.vma_public_ga_only and not is_public_ga_path(request.url.path):
            return JSONResponse(
                status_code=404,
                content=error_payload(
                    "not_found_error",
                    "This capability is not available in the Votrix public beta.",
                    code="capability_not_available",
                    request_id=getattr(request.state, "request_id", None),
                ),
            )
        return await call_next(request)

    if settings.vma_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.vma_cors_origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "Last-Event-ID",
                "request-id",
                "x-request-id",
                "anthropic-beta",
                "anthropic-version",
                "votrix-managed-agents-beta",
                "x-api-key",
            ],
            expose_headers=[
                "Content-Disposition",
                "Retry-After",
                "request-id",
                "x-request-id",
                "X-RateLimit-Limit",
                "X-RateLimit-Remaining",
                "X-RateLimit-Reset",
                "X-Quota-Metric",
                "X-Quota-Limit",
                "X-Quota-Remaining",
                "X-Quota-Reset",
            ],
        )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        supplied = request.headers.get("request-id") or request.headers.get("x-request-id")
        request_id = supplied if supplied and re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", supplied) else f"req_{uuid4().hex}"
        request.state.request_id = request_id
        started_at = perf_counter()
        clear_contextvars()
        bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        except Exception as exc:
            await _record_request_completion(
                request,
                status_code=500,
                duration_ms=(perf_counter() - started_at) * 1000,
                enabled=settings.vma_governance_enabled,
            )
            logger.error(
                "unhandled_request_error",
                request_id=request_id,
                error_type=exc.__class__.__name__,
                exc_info=exc,
            )
            headers = {
                "request-id": request_id,
                "x-request-id": request_id,
            }
            if decision := getattr(request.state, "rate_limit_decision", None):
                from app.governance_runtime import rate_limit_headers

                headers.update(rate_limit_headers(decision))
            return JSONResponse(
                status_code=500,
                content=error_payload(
                    "api_error",
                    "An unexpected error occurred",
                    code="internal_error",
                    request_id=request_id,
                ),
                headers=headers,
            )
        finally:
            clear_contextvars()

        if decision := getattr(request.state, "rate_limit_decision", None):
            from app.governance_runtime import rate_limit_headers

            for name, value in rate_limit_headers(decision).items():
                response.headers[name] = value
        response.headers["request-id"] = request_id
        response.headers["x-request-id"] = request_id
        await _record_request_completion(
            request,
            status_code=response.status_code,
            duration_ms=(perf_counter() - started_at) * 1000,
            enabled=settings.vma_governance_enabled,
        )
        return response

    app.include_router(api_keys.router)
    app.include_router(agents.router)
    app.include_router(environments.router)
    app.include_router(sessions.router)
    app.include_router(files.router)
    app.include_router(skills.router)
    app.include_router(model_providers.router)
    app.include_router(generic_resources.router)

    @app.get("/health", tags=["health"], response_model=HealthResponse)
    async def health():
        return {"status": "ok"}

    @app.get("/health/db", tags=["health"], response_model=DatabaseHealthResponse)
    async def health_db():
        from sqlalchemy import text

        from app.db.engine import session_scope

        async with session_scope() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ok", "db": "ok"}

    @app.get(
        "/v1/capabilities",
        tags=["capabilities"],
        response_model=CapabilityManifestResponse,
    )
    async def capabilities():
        return capability_manifest()

    original_openapi = app.openapi
    public_schema: dict | None = None

    def openapi():
        nonlocal public_schema
        schema = original_openapi()
        if not settings.vma_public_ga_only:
            return schema
        if public_schema is None:
            public_schema = public_ga_openapi(schema)
        return public_schema

    app.openapi = openapi

    return app


async def _record_request_completion(
    request: Request,
    *,
    status_code: int,
    duration_ms: float,
    enabled: bool,
) -> None:
    organization = getattr(request.state, "current_organization", None)
    if not enabled or organization is None:
        return

    route = request.scope.get("route")
    route_path = getattr(route, "path", None) or request.url.path
    resource_id = next(
        (str(value) for value in request.path_params.values() if value is not None),
        None,
    )
    if status_code < 400:
        outcome = "success"
    elif status_code in {401, 403, 429}:
        outcome = "denied"
    else:
        outcome = "failure"

    try:
        from app.governance_runtime import governance_service

        await governance_service().record_audit(
            organization.id,
            actor_type=organization.source,
            actor_id=organization.api_key_id,
            action="api.request.complete",
            outcome=outcome,
            resource_type="http_endpoint",
            resource_id=resource_id,
            request_id=getattr(request.state, "request_id", None),
            data={
                "method": request.method,
                "path": route_path,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 3),
            },
        )
    except Exception:
        # Completion telemetry must never turn an otherwise valid API response
        # into a failure. Request authorization remains fail-closed earlier in
        # the dependency chain.
        logger.exception(
            "request_completion_audit_failed",
            request_id=getattr(request.state, "request_id", None),
            status_code=status_code,
        )
