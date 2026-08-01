"""Memory Store HTTP lifecycle and native E2B Session attachment."""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.deps import get_db
from app.runtime.engine import _system_prompt
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


@pytest_asyncio.fixture
def headers(org):
    return {"x-organization-id": org, "x-api-key": "anything"}


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


async def test_update_renames_metadata_without_replacing_the_provider_volume(
    client, headers, volumes
):
    created = (await create_store(
        client,
        headers,
        metadata={"role": "content-creator", "remove": "yes"},
    )).json()

    response = await client.post(
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
    client, headers, agent, environment
):
    store = (await create_store(client, headers)).json()
    await client.post(f"/v1/memory_stores/{store['id']}/archive", headers=headers)

    response = await attach_store(
        client, headers, agent, environment, store["id"]
    )

    assert response.status_code == 409


async def test_an_attached_store_is_archived_instead_of_destroyed(
    client, headers, volumes, agent, environment
):
    store = (await create_store(client, headers)).json()
    await attach_store(client, headers, agent, environment, store["id"])

    response = await client.delete(
        f"/v1/memory_stores/{store['id']}", headers=headers
    )

    assert response.status_code == 409
    assert volumes.destroyed == []


async def test_an_unused_store_destroys_its_provider_volume(
    client, headers, volumes
):
    store = (await create_store(client, headers, name="Temporary")).json()

    response = await client.delete(
        f"/v1/memory_stores/{store['id']}", headers=headers
    )

    assert response.status_code == 200
    assert volumes.destroyed[0]["memory_store_id"] == store["id"]
    assert (
        await client.get(f"/v1/memory_stores/{store['id']}", headers=headers)
    ).status_code == 404


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
