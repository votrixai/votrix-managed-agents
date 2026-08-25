"""ASGI entry point for VMA's IAM-protected provisioning surface."""

from fastapi import FastAPI

from app.routers import control_plane_organizations, health
from app.server import _install_error_handlers


def create_control_plane_app() -> FastAPI:
    app = FastAPI(
        title="Votrix Managed Agents Control Plane",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(health.router)
    app.include_router(control_plane_organizations.router)
    _install_error_handlers(app)
    return app


app = create_control_plane_app()
