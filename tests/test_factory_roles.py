from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient
import pytest

from app.config import get_settings
from app.factory import create_app


def _configure_role(monkeypatch, role: str | None, *, sandbox_provider: str = "state") -> None:
    if role is None:
        monkeypatch.delenv("VMA_SERVICE_ROLE", raising=False)
    else:
        monkeypatch.setenv("VMA_SERVICE_ROLE", role)
    monkeypatch.setenv("VMA_EMBEDDED_WORKER_ENABLED", "true")
    monkeypatch.setenv("VMA_WORKER_CONCURRENCY", "1")
    monkeypatch.setenv("VMA_SANDBOX_PROVIDER", sandbox_provider)
    if sandbox_provider == "e2b":
        monkeypatch.setenv("E2B_API_KEY", "e2b_test")
    get_settings.cache_clear()


async def test_worker_role_exposes_only_health_and_starts_embedded_worker(monkeypatch):
    _configure_role(monkeypatch, "worker")
    worker_started = asyncio.Event()

    async def fake_run_worker(*, stop_event, **_kwargs):
        worker_started.set()
        await stop_event.wait()

    monkeypatch.setattr("app.worker.run_worker", fake_run_worker)
    app = create_app()

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(worker_started.wait(), timeout=1)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert (await client.get("/health/db")).status_code == 200
            assert (await client.get("/v1/agents")).status_code == 404
            assert (await client.get("/v1/sessions")).status_code == 404
            assert (await client.get("/openapi.json")).status_code == 404


async def test_api_role_mounts_business_routes_without_background_execution(monkeypatch):
    _configure_role(monkeypatch, "api", sandbox_provider="e2b")
    background_starts: list[str] = []

    async def fake_run_worker(**_kwargs):
        background_starts.append("worker")

    async def fake_run_janitor(_stop_event):
        background_starts.append("janitor")

    monkeypatch.setattr("app.worker.run_worker", fake_run_worker)
    monkeypatch.setattr("app.runtime.sandbox_lifecycle.run_sandbox_janitor", fake_run_janitor)
    app = create_app()

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        assert background_starts == []

    route_paths = set(app.openapi()["paths"])
    assert "/v1/agents" in route_paths
    assert "/v1/sessions" in route_paths
    assert "/health" in route_paths


async def test_combined_role_remains_default_and_starts_workers(monkeypatch):
    _configure_role(monkeypatch, None)
    worker_started = asyncio.Event()

    async def fake_run_worker(*, stop_event, **_kwargs):
        worker_started.set()
        await stop_event.wait()

    monkeypatch.setattr("app.worker.run_worker", fake_run_worker)
    app = create_app()

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(worker_started.wait(), timeout=1)

    route_paths = set(app.openapi()["paths"])
    assert get_settings().vma_service_role == "combined"
    assert "/v1/agents" in route_paths
    assert "/v1/sessions" in route_paths


async def test_pg_notify_preview_broker_requires_postgres(monkeypatch):
    _configure_role(monkeypatch, "api")
    monkeypatch.setenv("VMA_PREVIEW_BROKER", "pg_notify")
    get_settings.cache_clear()
    app = create_app()

    with pytest.raises(RuntimeError, match="requires a PostgreSQL DATABASE_URL"):
        async with app.router.lifespan_context(app):
            pass
