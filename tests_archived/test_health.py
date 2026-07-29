from importlib.metadata import version

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.factory import create_app


def test_public_build_id_defaults_to_dev_when_missing_or_blank(monkeypatch) -> None:
    monkeypatch.delenv("VMA_PUBLIC_BUILD_ID", raising=False)
    assert Settings(_env_file=None).vma_public_build_id == "dev"

    monkeypatch.setenv("VMA_PUBLIC_BUILD_ID", "   ")
    assert Settings(_env_file=None).vma_public_build_id == "dev"


@pytest.mark.parametrize(
    "build_id",
    (
        "contains spaces",
        "contains/slash",
        "secret=value",
        "x" * 129,
    ),
)
def test_public_build_id_rejects_unsafe_values(monkeypatch, build_id: str) -> None:
    monkeypatch.setenv("VMA_PUBLIC_BUILD_ID", build_id)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


async def test_health_routes_expose_package_version_and_public_build(monkeypatch) -> None:
    monkeypatch.setenv("VMA_SERVICE_ROLE", "api")
    monkeypatch.setenv("VMA_EMBEDDED_WORKER_ENABLED", "false")
    monkeypatch.setenv("VMA_WORK_DISPATCH_MODE", "poll")
    monkeypatch.setenv("VMA_PUBLIC_BUILD_ID", "abc1234")
    get_settings.cache_clear()
    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        health = await client.get("/health")
        database_health = await client.get("/health/db")

    package_version = version("votrix-managed-agents")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "version": package_version,
        "build": "abc1234",
    }
    assert database_health.status_code == 200
    assert database_health.json() == {
        "status": "ok",
        "version": package_version,
        "build": "abc1234",
        "db": "ok",
    }
