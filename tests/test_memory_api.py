"""Memory Store HTTP lifecycle and native E2B Session attachment."""

from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.errors import InvalidRequest
from app.routers.deps import get_db
from app.runtime.engine import _system_prompt
from app.services import memory as memory_service
from app.utils.sandbox import Sandbox
from app.utils.volume import memory_mount_path


@pytest_asyncio.fixture
async def client(db, volumes, sandboxes):
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http:
        yield http
    app.dependency_overrides.clear()


async def create_store(client, headers, **body):
    body.setdefault("name", "Content Creator")
    return await client.post("/v1/memory_stores", headers=headers, json=body)


async def attach_store(
    client,
    headers,
    agent,
    environment,
    memory_store_id,
    **resource,
):
    return await client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "agent_id": agent.id,
            "environment_id": environment.id,
            "resources": [
                {
                    "type": "memory_store",
                    "memory_store_id": memory_store_id,
                    **resource,
                }
            ],
        },
    )


async def test_create_provisions_one_provider_volume_and_hides_its_locator(
    client, headers, volumes
):
    response = await create_store(
        client,
        headers,
        description="Persistent brand context",
        metadata={"role": "content-creator"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"].startswith("memstore_")
    assert body["type"] == "memory_store"
    assert body["metadata"] == {"role": "content-creator"}
    assert volumes.created[0]["memory_store_id"] == body["id"]
    assert volumes.created[0]["volume_name"].startswith("vma-local-memstore-")
    for private in ("organization_id", "volume_provider", "volume_locator"):
        assert private not in body


async def test_provider_failure_is_a_503_and_failed_rows_stay_out_of_lists(
    client, headers, volumes
):
    volumes.create_error = RuntimeError("private beta unavailable")

    response = await create_store(client, headers)

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "memory_store_unavailable"
    listed = await client.get("/v1/memory_stores", headers=headers)
    assert listed.json()["data"] == []


async def test_patch_updates_store_attributes_without_replacing_the_provider_volume(
    client, headers, volumes
):
    created = (await create_store(
        client,
        headers,
        metadata={"role": "content-creator", "remove": "yes"},
    )).json()

    response = await client.patch(
        f"/v1/memory_stores/{created['id']}",
        headers=headers,
        json={
            "name": "Editorial Memory",
            "metadata": {"remove": None, "project": "launch"},
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Editorial Memory"
    assert response.json()["metadata"] == {
        "role": "content-creator",
        "project": "launch",
    }
    assert len(volumes.created) == 1


async def test_put_and_delete_an_unmounted_store_file(
    client, headers, volumes
):
    store = (await create_store(client, headers, name="Project Memory")).json()
    content = b"Durable project context.\n\x00"

    response = await client.put(
        f"/v1/memory_stores/{store['id']}/files/notes/context.bin",
        headers={**headers, "content-type": "application/octet-stream"},
        content=content,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "type": "memory_store_file",
        "memory_store_id": store["id"],
        "path": "notes/context.bin",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    assert volumes.files[store["id"]] == {"/notes/context.bin": content}

    deleted = await client.delete(
        f"/v1/memory_stores/{store['id']}/files/notes/context.bin",
        headers=headers,
    )
    repeated = await client.delete(
        f"/v1/memory_stores/{store['id']}/files/notes/context.bin",
        headers=headers,
    )

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert repeated.status_code == 204
    assert volumes.files[store["id"]] == {}


async def test_store_file_mutation_uses_an_idle_mounted_sandbox(
    client,
    db,
    org,
    headers,
    volumes,
    monkeypatch,
    agent,
    environment,
):
    store = (await create_store(client, headers)).json()
    session = (
        await attach_store(client, headers, agent, environment, store["id"])
    ).json()
    calls: list[tuple[str, str, bytes | None]] = []

    class MountedSandbox:
        async def write_bytes(self, path, content):
            calls.append(("write", path, bytes(content)))

        async def remove_file(self, path):
            calls.append(("remove", path, None))

    def _from_id(cls, sandbox_id, session_id, organization_id):
        assert sandbox_id == "sbx_fake"
        assert session_id == session["id"]
        assert organization_id == org
        return MountedSandbox()

    monkeypatch.setattr(Sandbox, "from_id", classmethod(_from_id))

    written = await client.put(
        f"/v1/memory_stores/{store['id']}/files/context.md",
        headers={**headers, "content-type": "application/octet-stream"},
        content=b"Updated through the API",
    )
    deleted = await client.delete(
        f"/v1/memory_stores/{store['id']}/files/context.md",
        headers=headers,
    )

    assert written.status_code == 200, written.text
    assert deleted.status_code == 204, deleted.text
    assert calls == [
        (
            "write",
            "/mnt/memory/content-creator/context.md",
            b"Updated through the API",
        ),
        ("remove", "/mnt/memory/content-creator/context.md", None),
    ]
    assert volumes.standalone_writes == []
    assert volumes.standalone_removes == []


async def test_store_file_mutation_is_rejected_while_an_attached_session_is_busy(
    client,
    db,
    org,
    headers,
    volumes,
    agent,
    environment,
):
    from app.db.queries import sessions as sessions_q

    store = (await create_store(client, headers)).json()
    session = (
        await attach_store(client, headers, agent, environment, store["id"])
    ).json()
    row = await sessions_q.get_session(
        db,
        session_id=session["id"],
        organization_id=org,
    )
    row.status = "running"
    await db.commit()

    response = await client.put(
        f"/v1/memory_stores/{store['id']}/files/context.md",
        headers={**headers, "content-type": "application/octet-stream"},
        content=b"racy update",
    )

    assert response.status_code == 409
    assert "busy Session" in response.json()["error"]["message"]
    assert volumes.standalone_writes == []


async def test_archived_store_file_mutation_is_rejected(
    client, db, org, headers, volumes
):
    store = (await create_store(client, headers)).json()
    await memory_service.archive_memory_store(
        db,
        memory_store_id=store["id"],
        organization_id=org,
    )

    response = await client.put(
        f"/v1/memory_stores/{store['id']}/files/context.md",
        headers={**headers, "content-type": "application/octet-stream"},
        content=b"blocked",
    )

    assert response.status_code == 409
    assert volumes.standalone_writes == []


async def test_store_file_provider_failure_is_a_503(client, headers, volumes):
    store = (await create_store(client, headers)).json()
    volumes.write_error = RuntimeError("volume content API unavailable")

    response = await client.put(
        f"/v1/memory_stores/{store['id']}/files/context.md",
        headers={**headers, "content-type": "application/octet-stream"},
        content=b"not persisted",
    )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "memory_store_unavailable"
    assert volumes.files[store["id"]] == {}


async def test_store_file_size_limit_returns_413(
    client, headers, volumes, monkeypatch
):
    store = (await create_store(client, headers)).json()
    monkeypatch.setattr(memory_service, "MAX_MEMORY_STORE_FILE_BYTES", 3)

    response = await client.put(
        f"/v1/memory_stores/{store['id']}/files/context.md",
        headers={**headers, "content-type": "application/octet-stream"},
        content=b"four",
    )

    assert response.status_code == 413
    assert volumes.standalone_writes == []


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.md",
        "../escape.md",
        "notes/./context.md",
        "notes//context.md",
        "notes/context.md/",
        "notes/zero\u200bwidth.md",
        "e\u0301.md",
    ],
)
def test_memory_store_file_path_validation(path):
    with pytest.raises(InvalidRequest):
        memory_service.normalize_memory_store_file_path(path)


async def test_update_rejects_an_explicitly_null_name(client, headers):
    store = (await create_store(client, headers)).json()

    response = await client.post(
        f"/v1/memory_stores/{store['id']}",
        headers=headers,
        json={"name": None},
    )

    assert response.status_code == 422


async def test_update_treats_an_empty_metadata_value_as_a_delete(client, headers):
    store = (
        await create_store(client, headers, metadata={"keep": "yes", "drop": "soon"})
    ).json()

    response = await client.post(
        f"/v1/memory_stores/{store['id']}",
        headers=headers,
        json={"metadata": {"drop": ""}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["metadata"] == {"keep": "yes"}


async def test_a_store_mounts_at_creation_and_is_returned_as_a_session_resource(
    client, headers, volumes, sandboxes, agent, environment
):
    store = (await create_store(
        client,
        headers,
        description="Brand voice and active projects",
    )).json()

    response = await attach_store(
        client,
        headers,
        agent,
        environment,
        store["id"],
        instructions="Read context.md before content work.",
    )

    assert response.status_code == 201, response.text
    expected_path = "/mnt/memory/content-creator"
    mounted = sandboxes[0]["memory_mounts"]
    assert [(item.mount_path, item.volume_name) for item in mounted] == [
        (expected_path, volumes.created[0]["volume_name"])
    ]
    resource = response.json()["resources"][0]
    assert resource == {
        "id": resource["id"],
        "type": "memory_store",
        "memory_store_id": store["id"],
        "access": "read_write",
        "instructions": "Read context.md before content work.",
        "mount_path": expected_path,
        "name": "Content Creator",
        "description": "Brand voice and active projects",
        "created_at": resource["created_at"],
        "updated_at": resource["updated_at"],
    }


async def test_file_and_memory_resources_are_attached_together(
    client, db, org, headers, sandboxes, agent, environment
):
    from app.db.queries import files as files_q

    file = await files_q.create_file(
        db,
        organization_id=org,
        filename="brief.md",
        storage_key="files/brief.md",
        mime_type="text/markdown",
        size_bytes=5,
    )
    await db.commit()
    store = (await create_store(client, headers, name="Project Memory")).json()

    response = await client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "agent_id": agent.id,
            "environment_id": environment.id,
            "resources": [
                {"type": "file", "file_id": file.id, "path": "brief.md"},
                {"type": "memory_store", "memory_store_id": store["id"]},
            ],
        },
    )

    assert response.status_code == 201, response.text
    assert sandboxes[0]["files"] == [(file.id, "brief.md")]
    assert sandboxes[0]["memory_mounts"][0].mount_path == (
        "/mnt/memory/project-memory"
    )
    resources = {resource["type"]: resource for resource in response.json()["resources"]}
    assert resources["file"]["mount_path"] == "/home/user/uploads/brief.md"
    assert resources["memory_store"]["mount_path"] == "/mnt/memory/project-memory"


async def test_attachment_snapshots_name_and_mount_path(
    client, headers, agent, environment
):
    store = (await create_store(client, headers)).json()
    session = (
        await attach_store(client, headers, agent, environment, store["id"])
    ).json()

    await client.post(
        f"/v1/memory_stores/{store['id']}",
        headers=headers,
        json={"name": "Renamed Later"},
    )
    retrieved = (
        await client.get(f"/v1/sessions/{session['id']}", headers=headers)
    ).json()

    resource = retrieved["resources"][0]
    assert resource["name"] == "Content Creator"
    assert resource["mount_path"] == "/mnt/memory/content-creator"


async def test_read_only_is_rejected_instead_of_being_falsely_enforced(
    client, headers, sandboxes, agent, environment
):
    store = (await create_store(client, headers)).json()

    response = await attach_store(
        client,
        headers,
        agent,
        environment,
        store["id"],
        access="read_only",
    )

    assert response.status_code == 409
    assert "not supported" in response.json()["error"]["message"]
    assert sandboxes == []


async def test_archived_store_cannot_be_attached(
    client, db, org, headers, agent, environment
):
    store = (await create_store(client, headers)).json()
    await memory_service.archive_memory_store(
        db,
        memory_store_id=store["id"],
        organization_id=org,
    )

    response = await attach_store(
        client, headers, agent, environment, store["id"]
    )

    assert response.status_code == 409


async def test_store_level_archive_and_delete_are_not_public(
    client, headers, volumes
):
    store = (await create_store(client, headers, name="Temporary")).json()

    archived = await client.post(
        f"/v1/memory_stores/{store['id']}/archive", headers=headers
    )
    deleted = await client.delete(
        f"/v1/memory_stores/{store['id']}", headers=headers
    )

    assert archived.status_code == 404
    assert deleted.status_code == 405
    assert volumes.destroyed == []
    retrieved = await client.get(
        f"/v1/memory_stores/{store['id']}", headers=headers
    )
    assert retrieved.status_code == 200


async def test_two_store_names_that_make_one_slug_are_rejected(
    client, headers, agent, environment
):
    first = (await create_store(client, headers, name="Content Creator")).json()
    second = (await create_store(client, headers, name="content---creator")).json()

    response = await client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "agent_id": agent.id,
            "environment_id": environment.id,
            "resources": [
                {"type": "memory_store", "memory_store_id": first["id"]},
                {"type": "memory_store", "memory_store_id": second["id"]},
            ],
        },
    )

    assert response.status_code == 409
    assert "same mount path" in response.json()["error"]["message"]


async def test_a_session_may_attach_at_most_eight_stores(
    client, headers, sandboxes, agent, environment
):
    stores = [
        (await create_store(client, headers, name=f"Memory {index}")).json()
        for index in range(9)
    ]

    response = await client.post(
        "/v1/sessions",
        headers=headers,
        json={
            "agent_id": agent.id,
            "environment_id": environment.id,
            "resources": [
                {"type": "memory_store", "memory_store_id": store["id"]}
                for store in stores
            ],
        },
    )

    assert response.status_code == 409
    assert "at most 8" in response.json()["error"]["message"]
    assert sandboxes == []


def test_system_prompt_names_the_exact_persistent_path():
    prompt = _system_prompt(
        "Be useful.",
        [],
        [
            {
                "name": "Content Creator",
                "description": "Brand context",
                "instructions": "Read context.md first.",
                "access": "read_write",
                "mount_path": "/mnt/memory/content-creator",
            }
        ],
    )

    assert "## Memory Stores" in prompt
    assert "`/mnt/memory/content-creator`" in prompt
    assert "Read context.md first." in prompt


def test_non_latin_names_receive_a_safe_nonempty_mount_path():
    assert memory_mount_path("内容创作", "memstore_0123456789abcdef") == (
        "/mnt/memory/memory-456789abcdef"
    )
