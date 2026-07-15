import os
import hashlib

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.db.engine import get_engine, reset_engine_for_tests
from app.db.models import Base
from app.workspace import default_workspace, set_current_workspace

TEST_HEADERS = {
    "anthropic-beta": "managed-agents-2026-04-01",
    "anthropic-version": "2023-06-01",
}

VOTRIX_MANAGED_AGENTS_HEADERS = {
    "votrix-managed-agents-beta": "votrix-managed-agents-2026-04-01",
}


@pytest.fixture(autouse=True)
async def test_database(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    # Never let a developer's real .env provision E2B during the test suite.
    monkeypatch.setenv("VMA_SANDBOX_PROVIDER", "state")
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://storage.example.com")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setenv("S3_BUCKET_NAME", "vma-test")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("VMA_REQUIRE_BETA_HEADER", "true")
    monkeypatch.setenv("VMA_REQUIRE_ANTHROPIC_VERSION_HEADER", "true")
    object_store: dict[str, tuple[bytes, str]] = {}

    async def fake_save_file_bytes(data, mime_type, *, namespace, filename, category="general", workspace_id=None):
        from app.storage import StoredObject, object_key

        content_type = mime_type or "application/octet-stream"
        sha256 = hashlib.sha256(data).hexdigest()
        key = object_key(
            namespace=namespace,
            category=category,
            filename=filename,
            content_sha256=sha256,
            workspace_id=workspace_id,
        )
        object_store[key] = (data, content_type)
        return StoredObject(
            backend="s3",
            key=key,
            content_type=content_type,
            size_bytes=len(data),
            sha256=sha256,
        )

    async def fake_download_file_with_type(key):
        return object_store[key]

    async def fake_delete_file(key):
        object_store.pop(key, None)

    async def fake_copy_file(source_key, destination_key, *, content_type=None):
        data, existing_content_type = object_store[source_key]
        object_store[destination_key] = (data, content_type or existing_content_type)

    async def fake_get_file_info(key):
        data, content_type = object_store[key]
        return {"ContentLength": len(data), "ContentType": content_type}

    async def fake_create_presigned_upload_url(key, mime_type, *, expires_in=900):
        object_store.setdefault(key, (b"", mime_type))
        return f"https://upload.example.com/{key}?expires={expires_in}"

    monkeypatch.setattr("app.storage.save_file_bytes", fake_save_file_bytes)
    monkeypatch.setattr("app.storage.download_file_with_type", fake_download_file_with_type)
    monkeypatch.setattr("app.storage.delete_file", fake_delete_file)
    monkeypatch.setattr("app.storage.copy_file", fake_copy_file)
    monkeypatch.setattr("app.storage.get_file_info", fake_get_file_info)
    monkeypatch.setattr("app.storage.create_presigned_upload_url", fake_create_presigned_upload_url)
    monkeypatch.setattr("app.routers.files.save_file_bytes", fake_save_file_bytes)
    monkeypatch.setattr("app.routers.files.download_file_with_type", fake_download_file_with_type)
    monkeypatch.setattr("app.routers.files.delete_stored_file", fake_delete_file)
    monkeypatch.setattr("app.routers.files.copy_file", fake_copy_file)
    monkeypatch.setattr("app.routers.files.get_file_info", fake_get_file_info)
    monkeypatch.setattr("app.routers.files.create_presigned_upload_url", fake_create_presigned_upload_url)
    monkeypatch.setattr("app.routers.skills.save_file_bytes", fake_save_file_bytes)
    monkeypatch.setattr("app.routers.skills.download_file_with_type", fake_download_file_with_type)

    from app.runtime.runner import _execute_local

    async def fake_deepagents_executor(
        version,
        history,
        environment_config=None,
        *,
        runtime_context=None,
        **_kwargs,
    ):
        return await _execute_local(
            version,
            history,
            environment_config,
            runtime_context=runtime_context,
        )

    monkeypatch.setattr("app.runtime.deepagents_engine.execute_deep_agent", fake_deepagents_executor)
    set_current_workspace(default_workspace())
    get_settings.cache_clear()
    await reset_engine_for_tests()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await reset_engine_for_tests()
    get_settings.cache_clear()
    set_current_workspace(default_workspace())
    os.environ.pop("DATABASE_URL", None)


@pytest.fixture
async def client():
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
