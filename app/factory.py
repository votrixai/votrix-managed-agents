from contextlib import asynccontextmanager

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
    yield


def create_app(*, auth_provider: AuthProvider | None = None) -> FastAPI:
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
