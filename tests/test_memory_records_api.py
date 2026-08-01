"""CMA Memory document, version, SDK, and Runtime synchronization contracts."""

from __future__ import annotations

import hashlib

import pytest_asyncio
from anthropic import AsyncAnthropic
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import ToolMessage

from app.main import app
from app.routers.deps import get_db
from app.runtime.engine import _translate
from app.services import memory_records
from app.utils.sandbox import OutputFile


@pytest_asyncio.fixture
async def client(db, volumes, sandboxes):
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http:
        yield http
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
def headers(org):
    return {"x-organization-id": org, "x-api-key": "test-secret"}


async def _create_store(client, headers, name="Project Memory"):
    response = await client.post(
        "/v1/memory_stores",
        headers=headers,
        json={"name": name, "description": "Persistent project context."},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_cma_memory_crud_versions_redaction_and_volume_mirror(
    client, headers, volumes
):
    store = await _create_store(client, headers)
    created_response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=headers,
        json={"path": "/preferences/style.md", "content": "Use short headings."},
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()
    assert created["type"] == "memory"
    assert created["content"] == "Use short headings."
    assert created["content_sha256"] == hashlib.sha256(
        b"Use short headings."
    ).hexdigest()
    assert volumes.files[store["id"]] == {
        "/preferences/style.md": "Use short headings."
    }
    first_version_id = created["memory_version_id"]

    listed = await client.get(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"][0]["content"] is None

    retrieved = await client.get(
        f"/v1/memory_stores/{store['id']}/memories/{created['id']}",
        headers=headers,
    )
    assert retrieved.json()["content"] == "Use short headings."

    idempotent = await client.post(
        f"/v1/memory_stores/{store['id']}/memories/{created['id']}",
        headers=headers,
        json={
            "content": "Use short headings.",
            "precondition": {
                "type": "content_sha256",
                "content_sha256": "0" * 64,
            },
        },
    )
    assert idempotent.status_code == 200, idempotent.text
    assert idempotent.json()["memory_version_id"] == first_version_id

    stale = await client.post(
        f"/v1/memory_stores/{store['id']}/memories/{created['id']}",
        headers=headers,
        json={
            "content": "Different content",
            "precondition": {
                "type": "content_sha256",
                "content_sha256": "0" * 64,
            },
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["type"] == "memory_precondition_failed_error"

    updated_response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories/{created['id']}",
        headers=headers,
        json={
            "path": "/archive/style.md",
            "content": "Use two-word headings.",
            "precondition": {
                "type": "content_sha256",
                "content_sha256": created["content_sha256"],
            },
        },
    )
    assert updated_response.status_code == 200, updated_response.text
    updated = updated_response.json()
    assert updated["id"] == created["id"]
    assert updated["path"] == "/archive/style.md"
    assert updated["memory_version_id"] != first_version_id
    assert volumes.files[store["id"]] == {
        "/archive/style.md": "Use two-word headings."
    }

    versions_response = await client.get(
        f"/v1/memory_stores/{store['id']}/memory_versions",
        headers=headers,
        params={"memory_id": created["id"], "view": "full"},
    )
    assert versions_response.status_code == 200, versions_response.text
    versions = versions_response.json()["data"]
    assert [item["operation"] for item in versions] == ["modified", "created"]
    assert versions[0]["content"] == "Use two-word headings."
    assert versions[0]["created_by"]["type"] == "api_actor"
    assert versions[0]["created_by"]["api_key_id"].startswith("apikey_")

    current_redact = await client.post(
        f"/v1/memory_stores/{store['id']}/memory_versions/"
        f"{updated['memory_version_id']}/redact",
        headers=headers,
    )
    assert current_redact.status_code == 409

    historical_redact = await client.post(
        f"/v1/memory_stores/{store['id']}/memory_versions/{first_version_id}/redact",
        headers=headers,
    )
    assert historical_redact.status_code == 200, historical_redact.text
    redacted = historical_redact.json()
    assert redacted["redacted_at"] is not None
    assert redacted["path"] is None
    assert redacted["content"] is None
    assert redacted["content_sha256"] is None

    wrong_delete = await client.delete(
        f"/v1/memory_stores/{store['id']}/memories/{created['id']}",
        headers=headers,
        params={"expected_content_sha256": created["content_sha256"]},
    )
    assert wrong_delete.status_code == 409

    deleted_response = await client.delete(
        f"/v1/memory_stores/{store['id']}/memories/{created['id']}",
        headers=headers,
        params={"expected_content_sha256": updated["content_sha256"]},
    )
    assert deleted_response.status_code == 200, deleted_response.text
    assert deleted_response.json() == {"id": created["id"], "type": "memory_deleted"}
    assert volumes.files[store["id"]] == {}

    versions_response = await client.get(
        f"/v1/memory_stores/{store['id']}/memory_versions",
        headers=headers,
        params={"memory_id": created["id"], "view": "full"},
    )
    versions = versions_response.json()["data"]
    assert [item["operation"] for item in versions] == [
        "deleted",
        "modified",
        "created",
    ]
    assert versions[0]["path"] == "/archive/style.md"
    assert versions[0]["content"] is None
    assert versions[0]["content_sha256"] is None


async def test_cma_path_depth_pagination_limits_and_archive_rules(
    client, headers
):
    store = await _create_store(client, headers)
    for path in (
        "/projects/foo/notes.md",
        "/projects/foo/todo.md",
        "/projects/readme.md",
        "/accounts/acme.md",
    ):
        response = await client.post(
            f"/v1/memory_stores/{store['id']}/memories",
            headers=headers,
            json={"path": path, "content": path},
        )
        assert response.status_code == 201, response.text

    first = await client.get(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=headers,
        params={"path_prefix": "/projects/", "depth": 1, "limit": 1},
    )
    assert first.status_code == 200, first.text
    assert [(item["type"], item["path"]) for item in first.json()["data"]] == [
        ("memory_prefix", "/projects/foo/")
    ]
    assert first.json()["next_page"].startswith("page_")

    second = await client.get(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=headers,
        params={
            "path_prefix": "/projects/",
            "depth": 1,
            "limit": 1,
            "page": first.json()["next_page"],
        },
    )
    assert [(item["type"], item["path"]) for item in second.json()["data"]] == [
        ("memory", "/projects/readme.md")
    ]
    assert second.json()["next_page"] is None

    invalid_depth = await client.get(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=headers,
        params={"depth": 2},
    )
    assert invalid_depth.status_code == 400

    invalid_prefix = await client.get(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=headers,
        params={"path_prefix": "/projects"},
    )
    assert invalid_prefix.status_code == 400

    invalid_path = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=headers,
        json={"path": "projects/no-leading-slash", "content": "bad"},
    )
    assert invalid_path.status_code == 422

    too_large = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=headers,
        json={"path": "/large.md", "content": "x" * (100 * 1024 + 1)},
    )
    assert too_large.status_code == 413

    archived = await client.post(
        f"/v1/memory_stores/{store['id']}/archive",
        headers=headers,
    )
    assert archived.status_code == 200
    still_readable = await client.get(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=headers,
    )
    assert still_readable.status_code == 200
    blocked = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=headers,
        json={"path": "/blocked.md", "content": "blocked"},
    )
    assert blocked.status_code == 409


async def test_official_anthropic_python_sdk_parses_memory_surface(
    db, org, volumes, sandboxes
):
    app.dependency_overrides[get_db] = lambda: db
    transport = ASGITransport(app=app)
    http = AsyncClient(transport=transport, base_url="http://test")
    sdk = AsyncAnthropic(
        api_key="sdk-secret",
        base_url="http://test",
        default_headers={"x-organization-id": org},
        http_client=http,
    )
    try:
        store = await sdk.beta.memory_stores.create(name="SDK Memory")
        extra_store = await sdk.beta.memory_stores.create(name="SDK Memory 2")
        stores_page = await sdk.beta.memory_stores.list(limit=1)
        assert stores_page.data[0].type == "memory_store"
        assert stores_page.next_page is not None
        next_stores_page = await stores_page.get_next_page()
        assert next_stores_page.data[0].type == "memory_store"

        updated_store = await sdk.beta.memory_stores.update(
            store.id,
            description="Updated through the official SDK",
            metadata={"contract": "memory"},
        )
        assert updated_store.description == "Updated through the official SDK"
        retrieved_store = await sdk.beta.memory_stores.retrieve(store.id)
        assert retrieved_store.metadata == {"contract": "memory"}

        memory = await sdk.beta.memory_stores.memories.create(
            store.id,
            path="/sdk/context.md",
            content="SDK-compatible content",
        )
        assert memory.type == "memory"
        assert memory.content == "SDK-compatible content"
        retrieved_memory = await sdk.beta.memory_stores.memories.retrieve(
            memory.id,
            memory_store_id=store.id,
        )
        assert retrieved_memory.content == "SDK-compatible content"

        page = await sdk.beta.memory_stores.memories.list(store.id)
        assert page.data[0].type == "memory"
        assert page.data[0].content is None

        updated = await sdk.beta.memory_stores.memories.update(
            memory.id,
            memory_store_id=store.id,
            content="Updated through SDK",
            precondition={
                "type": "content_sha256",
                "content_sha256": memory.content_sha256,
            },
        )
        versions = await sdk.beta.memory_stores.memory_versions.list(
            store.id,
            memory_id=memory.id,
            view="full",
        )
        assert versions.data[0].id == updated.memory_version_id
        assert versions.data[0].content == "Updated through SDK"

        historical = await sdk.beta.memory_stores.memory_versions.retrieve(
            memory.memory_version_id,
            memory_store_id=store.id,
        )
        assert historical.content == "SDK-compatible content"
        redacted = await sdk.beta.memory_stores.memory_versions.redact(
            historical.id,
            memory_store_id=store.id,
        )
        assert redacted.redacted_at is not None
        assert redacted.content is None
        assert redacted.path is None

        deleted_memory = await sdk.beta.memory_stores.memories.delete(
            memory.id,
            memory_store_id=store.id,
            expected_content_sha256=updated.content_sha256,
        )
        assert deleted_memory.type == "memory_deleted"

        deleted_store = await sdk.beta.memory_stores.delete(store.id)
        assert deleted_store.type == "memory_store_deleted"
        archived_store = await sdk.beta.memory_stores.archive(extra_store.id)
        assert archived_store.archived_at is not None
        await sdk.beta.memory_stores.delete(archived_store.id)
    finally:
        await sdk.close()
        app.dependency_overrides.clear()


async def test_provider_failure_does_not_commit_a_memory_head(
    client, headers, volumes
):
    store = await _create_store(client, headers)
    volumes.write_error = RuntimeError("provider write failed")

    failed = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=headers,
        json={"path": "/context.md", "content": "must not be indexed"},
    )
    assert failed.status_code == 503

    listed = await client.get(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=headers,
        params={"view": "full"},
    )
    assert listed.status_code == 200
    assert listed.json()["data"] == []
    assert volumes.files[store["id"]] == {}


async def test_runtime_files_become_session_attributed_versions(
    client, headers, db, agent, environment
):
    store = await _create_store(client, headers, name="Content Creator")
    session_response = await client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "agent_id": agent.id,
            "environment_id": environment.id,
            "resources": [
                {"type": "memory_store", "memory_store_id": store["id"]}
            ],
        },
    )
    assert session_response.status_code == 201, session_response.text
    session = session_response.json()
    resource = next(
        item for item in session["resources"] if item["type"] == "memory_store"
    )

    class MountedSandbox:
        def __init__(self):
            self.files = {"context.md": b"Brand voice: direct."}

        async def list_files(self, path, *, max_files, include_oversized):
            return [
                OutputFile(
                    path=name,
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
                for name, content in sorted(self.files.items())
            ][:max_files]

        async def read_bytes(self, path, *, max_bytes):
            relative = path.removeprefix(resource["mount_path"] + "/")
            content = self.files[relative]
            if len(content) > max_bytes:
                raise ValueError("too large")
            return content

    mounted = MountedSandbox()
    first = await memory_records.reconcile_session_memory_stores(
        db,
        session_id=session["id"],
        organization_id=headers["x-organization-id"],
        sandbox=mounted,
    )
    assert first.changed == 1

    memories = await client.get(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=headers,
        params={"view": "full"},
    )
    memory = memories.json()["data"][0]
    assert memory["path"] == "/context.md"
    assert memory["content"] == "Brand voice: direct."

    mounted.files["context.md"] = b"Brand voice: warm and direct."
    second = await memory_records.reconcile_session_memory_stores(
        db,
        session_id=session["id"],
        organization_id=headers["x-organization-id"],
        sandbox=mounted,
    )
    assert second.changed == 1

    mounted.files.clear()
    third = await memory_records.reconcile_session_memory_stores(
        db,
        session_id=session["id"],
        organization_id=headers["x-organization-id"],
        sandbox=mounted,
    )
    assert third.changed == 1

    versions = await client.get(
        f"/v1/memory_stores/{store['id']}/memory_versions",
        headers=headers,
        params={"memory_id": memory["id"], "session_id": session["id"], "view": "full"},
    )
    assert versions.status_code == 200, versions.text
    assert [item["operation"] for item in versions.json()["data"]] == [
        "deleted",
        "modified",
        "created",
    ]
    assert all(
        item["created_by"] == {"type": "session_actor", "session_id": session["id"]}
        for item in versions.json()["data"]
    )


async def test_runtime_hook_reconciles_after_successful_filesystem_mutators():
    emitted = []
    completed = []

    async def emit(kind, payload):
        emitted.append((kind, payload))

    async def tool_completed(name):
        completed.append(name)

    await _translate(
        ToolMessage(
            content="written",
            tool_call_id="tool-1",
            name="write_file",
        ),
        emit,
        {},
        {},
        set(),
        tool_completed,
    )
    await _translate(
        ToolMessage(
            content="read",
            tool_call_id="tool-2",
            name="read_file",
        ),
        emit,
        {},
        {},
        set(),
        tool_completed,
    )

    assert completed == ["write_file"]
    assert len(emitted) == 2
