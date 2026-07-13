import asyncio
from contextlib import asynccontextmanager, suppress

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.auth import AuthProvider, EnvApiKeyAuthProvider
from app.config import get_settings
from app.errors import install_error_handlers
from app.logging import setup as setup_logging
from app.routers import agents, environments, files, generic_resources, sessions, skills


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
    if str(settings.vma_sandbox_provider).strip().lower() == "e2b" and settings.e2b_api_key:
        from app.runtime.sandbox_lifecycle import run_sandbox_janitor

        janitor_task = asyncio.create_task(
            run_sandbox_janitor(janitor_stop),
            name="vma-e2b-janitor",
        )
    try:
        yield
    finally:
        janitor_stop.set()
        if janitor_task is not None:
            try:
                await asyncio.wait_for(janitor_task, timeout=5)
            except TimeoutError:
                janitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await janitor_task


def create_app(*, auth_provider: AuthProvider | None = None) -> FastAPI:
    # The packaged factory is also the local run.sh entrypoint. Load .env here
    # so dynamically configured provider credentials are available via their
    # api_key_env names; process-level Cloud Run variables still take priority.
    load_dotenv()
    app = FastAPI(title="Votrix Managed Agents", lifespan=lifespan, docs_url=None)
    app.state.auth_provider = auth_provider or EnvApiKeyAuthProvider()
    install_error_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(agents.router)
    app.include_router(environments.router)
    app.include_router(sessions.router)
    app.include_router(files.router)
    app.include_router(skills.router)
    app.include_router(generic_resources.router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/health/db")
    async def health_db():
        from sqlalchemy import text

        from app.db.engine import session_scope

        async with session_scope() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ok", "db": "ok"}

    @app.get("/docs", include_in_schema=False)
    async def scalar_docs():
        return HTMLResponse(
            """
<!doctype html>
<html>
<head><title>Votrix Managed Agents API</title><meta charset="utf-8"/></head>
<body>
<script id="api-reference" data-url="/openapi.json"></script>
<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
</body>
</html>
"""
        )

    return app
