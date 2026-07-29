"""ASGI entry point: `uvicorn app.main:app`."""

from app.server import create_app

app = create_app()
