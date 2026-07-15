import pytest
from fastapi import HTTPException

from app.config import get_settings
from app.db.engine import session_scope
from app.db.queries import resources as res_q
from app.routers import files as files_router
from app.routers import skills as skills_router
from tests.conftest import TEST_HEADERS


async def test_file_upload_denies_before_object_write_when_storage_quota_is_exhausted(
    client,
    monkeypatch,
):
    monkeypatch.setenv("VMA_WORKSPACE_STORAGE_BYTES", "0")
    get_settings.cache_clear()

    async def unexpected_save(*_args, **_kwargs):
        pytest.fail("object storage must not be written after quota denial")

    monkeypatch.setattr(files_router, "save_file_bytes", unexpected_save)

    response = await client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("denied.txt", b"not-empty", "text/plain")},
    )

    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "storage_quota_exceeded"
    assert response.headers["x-quota-metric"] == "storage_bytes"
    assert response.headers["x-quota-limit"] == "0"
    async with session_scope() as db:
        assert await res_q.list_resources(db, resource_type="file") == []


async def test_staged_file_completion_denies_before_copy_or_delete(
    client,
    monkeypatch,
):
    monkeypatch.setenv("VMA_WORKSPACE_STORAGE_BYTES", "0")
    get_settings.cache_clear()
    staged_key = (
        "workspaces/wrkspc_default/vma/staged-uploads/"
        "2026-07-15/obj_staged.txt"
    )

    async def staged_info(_key):
        return {"ContentLength": 4, "ContentType": "text/plain"}

    async def staged_download(_key):
        return b"data", "text/plain"

    async def unexpected_mutation(*_args, **_kwargs):
        pytest.fail("staged objects must remain untouched after quota denial")

    monkeypatch.setattr(files_router, "get_file_info", staged_info)
    monkeypatch.setattr(files_router, "download_file_with_type", staged_download)
    monkeypatch.setattr(files_router, "copy_file", unexpected_mutation)
    monkeypatch.setattr(files_router, "delete_stored_file", unexpected_mutation)

    response = await client.post(
        "/v1/files/complete",
        headers=TEST_HEADERS,
        json={"key": staged_key, "filename": "staged.txt"},
    )

    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "storage_quota_exceeded"
    async with session_scope() as db:
        assert await res_q.list_resources(db, resource_type="file") == []


async def test_skill_creation_denies_before_object_write_and_rolls_back_parent(
    client,
    monkeypatch,
):
    monkeypatch.setenv("VMA_WORKSPACE_STORAGE_BYTES", "0")
    get_settings.cache_clear()

    async def unexpected_save(*_args, **_kwargs):
        pytest.fail("skill objects must not be written after quota denial")

    monkeypatch.setattr(skills_router, "save_file_bytes", unexpected_save)

    response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={
            "files": (
                "skill/SKILL.md",
                b"---\nname: quota\ndescription: Quota test.\n---\nBody.",
                "text/markdown",
            )
        },
    )

    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "storage_quota_exceeded"
    async with session_scope() as db:
        assert await res_q.list_resources(db, resource_type="skill") == []
        assert await res_q.list_resources(db, resource_type="skill_version") == []


async def test_storage_quota_can_be_disabled_for_local_compatibility(client, monkeypatch):
    monkeypatch.setenv("VMA_GOVERNANCE_ENABLED", "false")
    monkeypatch.setenv("VMA_WORKSPACE_STORAGE_BYTES", "0")
    get_settings.cache_clear()

    response = await client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("allowed.txt", b"data", "text/plain")},
    )

    assert response.status_code == 201, response.text


async def test_storage_accounting_is_cumulative_and_workspace_scoped(client, monkeypatch):
    monkeypatch.setenv("VMA_API_KEYS", "key-a,key-b")
    monkeypatch.setenv("VMA_API_KEY_WORKSPACES", '{"key-a":"ws_a","key-b":"ws_b"}')
    monkeypatch.setenv("VMA_WORKSPACE_STORAGE_BYTES", "4")
    get_settings.cache_clear()
    headers_a = {**TEST_HEADERS, "x-api-key": "key-a"}
    headers_b = {**TEST_HEADERS, "x-api-key": "key-b"}

    first_a = await client.post(
        "/v1/files",
        headers=headers_a,
        files={"file": ("a.txt", b"aaaa", "text/plain")},
    )
    denied_a = await client.post(
        "/v1/files",
        headers=headers_a,
        files={"file": ("extra.txt", b"x", "text/plain")},
    )
    first_b = await client.post(
        "/v1/files",
        headers=headers_b,
        files={"file": ("b.txt", b"bbbb", "text/plain")},
    )

    assert first_a.status_code == 201, first_a.text
    assert denied_a.status_code == 429, denied_a.text
    assert first_b.status_code == 201, first_b.text


async def test_new_skill_version_is_denied_without_advancing_latest_version(
    client,
    monkeypatch,
):
    initial = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={
            "files": (
                "skill/SKILL.md",
                b"---\nname: versioned\ndescription: Versioned.\n---\nOne.",
                "text/markdown",
            )
        },
    )
    assert initial.status_code == 201, initial.text
    skill = initial.json()
    initial_version = skill["latest_version"]
    async with session_scope() as db:
        stored = await res_q.get_resource_version(
            db,
            resource_type="skill_version",
            parent_id=skill["id"],
            version=int(initial_version),
        )
        assert stored is not None
        initial_size = stored.size_bytes
    assert initial_size is not None

    monkeypatch.setenv("VMA_WORKSPACE_STORAGE_BYTES", str(initial_size))
    get_settings.cache_clear()

    async def unexpected_save(*_args, **_kwargs):
        pytest.fail("new skill version must not be stored after quota denial")

    monkeypatch.setattr(skills_router, "save_file_bytes", unexpected_save)
    denied = await client.post(
        f"/v1/skills/{skill['id']}/versions",
        headers=TEST_HEADERS,
        files={
            "files": (
                "skill/SKILL.md",
                b"---\nname: versioned\ndescription: Versioned.\n---\nTwo.",
                "text/markdown",
            )
        },
    )

    assert denied.status_code == 429, denied.text
    retrieved = await client.get(f"/v1/skills/{skill['id']}", headers=TEST_HEADERS)
    assert retrieved.status_code == 200, retrieved.text
    assert retrieved.json()["latest_version"] == initial_version
    versions = await client.get(
        f"/v1/skills/{skill['id']}/versions",
        headers=TEST_HEADERS,
    )
    assert versions.status_code == 200, versions.text
    assert len(versions.json()["data"]) == 1


async def test_direct_upload_reader_is_chunked_and_stops_at_configured_bound():
    class TrackingUpload:
        def __init__(self, content: bytes):
            self.content = content
            self.offset = 0
            self.read_sizes: list[int] = []

        async def read(self, size: int) -> bytes:
            assert size > 0
            self.read_sizes.append(size)
            chunk = self.content[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    limit = files_router.UPLOAD_READ_CHUNK_BYTES + 7
    upload = TrackingUpload(b"x" * (limit + 50))

    with pytest.raises(HTTPException) as exc_info:
        await files_router._read_upload_file_bounded(upload, max_bytes=limit)

    assert exc_info.value.status_code == 413
    assert upload.offset == limit + 1
    assert max(upload.read_sizes) <= files_router.UPLOAD_READ_CHUNK_BYTES
    assert len(upload.read_sizes) == 2
