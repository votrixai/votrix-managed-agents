from datetime import datetime, timedelta, timezone

from app.db.engine import session_scope
from app.db.models import ManagedResource
from app.db.queries import resources as res_q
from app.ids import new_id
from tests.conftest import TEST_HEADERS, TEST_ORGANIZATION_ID


PRIVATE_MEMORY_VERSION_FIELDS = {"actor", "path_key", "session_id", "snapshot"}


def _assert_public_memory_version(version: dict) -> None:
    assert PRIVATE_MEMORY_VERSION_FIELDS.isdisjoint(version)


async def _create_store(client):
    response = await client.post(
        "/v1/memory_stores",
        headers=TEST_HEADERS,
        json={"name": "Organization memory"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_memory_store_name_and_description_validation(client):
    invalid_payloads = [
        ({}, "name"),
        ({"name": ""}, "empty"),
        ({"name": "x" * 256}, "255"),
        ({"name": "bad\nname"}, "control"),
        ({"name": "Organization memory", "description": "x" * 1025}, "1024"),
    ]

    for payload, message in invalid_payloads:
        response = await client.post("/v1/memory_stores", headers=TEST_HEADERS, json=payload)

        assert response.status_code == 422, response.text
        assert message in response.json()["error"]["message"]

    response = await client.post(
        "/v1/memory_stores",
        headers=TEST_HEADERS,
        json={"name": "Organization memory", "description": "Useful account notes."},
    )
    assert response.status_code == 201, response.text
    store = response.json()

    response = await client.post(
        f"/v1/memory_stores/{store['id']}",
        headers=TEST_HEADERS,
        json={"name": "bad\tname"},
    )
    assert response.status_code == 422
    assert "control" in response.json()["error"]["message"]

    response = await client.post(
        f"/v1/memory_stores/{store['id']}",
        headers=TEST_HEADERS,
        json={"name": None, "description": "Null name is omitted."},
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Organization memory"
    assert response.json()["description"] == "Null name is omitted."


async def test_memory_routes_use_typed_openapi_request_models(client):
    response = await client.get("/openapi.json")
    assert response.status_code == 200, response.text
    spec = response.json()

    schemas = spec["components"]["schemas"]
    for schema_name in (
        "MemoryStoreCreateRequest",
        "MemoryStoreUpdateRequest",
        "MemoryCreateRequest",
        "MemoryUpdateRequest",
    ):
        assert schema_name in schemas

    create_memory_schema = spec["paths"]["/v1/memory_stores/{memory_store_id}/memories"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert create_memory_schema["$ref"].endswith("/MemoryCreateRequest")


async def test_memory_path_uniqueness_lookup_and_versions(client):
    store = await _create_store(client)

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={
            "path": ["accounts", "acme"],
            "content": "ACME prefers email.",
            "actor": "test",
        },
    )
    assert response.status_code == 201, response.text
    memory = response.json()
    assert memory["path"] == "/accounts/acme"
    assert memory["path_key"] == "accounts/acme"
    assert memory["version"] == 1
    assert memory["updated_by"] == "test"
    assert memory["content_size_bytes"] == len("ACME prefers email.".encode())

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={"path": "/accounts/acme", "content": "duplicate"},
    )
    assert response.status_code == 409

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memories/by_path",
        headers=TEST_HEADERS,
        params={"path": "/accounts/acme"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == memory["id"]

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}",
        headers=TEST_HEADERS,
        json={
            "if_version": 1,
            "content": "ACME prefers email and quarterly reviews.",
            "actor": "operator",
        },
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["version"] == 2
    assert updated["updated_by"] == "operator"

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}",
        headers=TEST_HEADERS,
        json={"if_version": 1, "content": "stale"},
    )
    assert response.status_code == 409

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}",
        headers=TEST_HEADERS,
        json={
            "if_version": 2,
            "path": "/accounts/acme-renamed",
            "content": "ACME renamed path.",
            "actor": "operator",
        },
    )
    assert response.status_code == 200, response.text
    renamed = response.json()
    assert renamed["version"] == 3
    assert renamed["path"] == "/accounts/acme-renamed"
    assert renamed["path_key"] == "accounts/acme-renamed"

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memories/by_path",
        headers=TEST_HEADERS,
        params={"path": "/accounts/acme"},
    )
    assert response.status_code == 404

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memories/by_path",
        headers=TEST_HEADERS,
        params={"path": "/accounts/acme-renamed"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == memory["id"]

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}/versions",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    versions = response.json()["data"]
    assert [version["version"] for version in versions] == [3, 2, 1]
    _assert_public_memory_version(versions[0])
    assert versions[0]["created_by"]["api_key_id"] == "operator"
    assert versions[0]["operation"] == "modified"


async def test_memory_create_requires_content(client):
    store = await _create_store(client)

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={"path": "/accounts/acme"},
    )

    assert response.status_code == 422, response.text
    assert "content" in response.json()["error"]["message"]


async def test_memory_update_noop_and_stale_precondition_match_do_not_create_version(client):
    store = await _create_store(client)
    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={"path": "/accounts/acme", "content": "same content"},
    )
    assert response.status_code == 201, response.text
    memory = response.json()

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}",
        headers=TEST_HEADERS,
        json={
            "content": "same content",
            "precondition": {"type": "content_sha256", "content_sha256": "stale"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 1

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}",
        headers=TEST_HEADERS,
        json={"content": "same content"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 1

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}/versions",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert [version["version"] for version in response.json()["data"]] == [1]


async def test_memory_update_normalizes_nullable_patch_fields(client):
    store = await _create_store(client)
    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={
            "path": "/accounts/acme",
            "content": "before",
            "actor": "original-actor",
            "metadata": {"remove": "me"},
        },
    )
    assert response.status_code == 201, response.text
    memory = response.json()

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}",
        headers=TEST_HEADERS,
        json={
            "path": None,
            "content": "after",
            "actor": None,
            "updated_by": None,
            "metadata": None,
        },
    )

    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["path"] == "/accounts/acme"
    assert updated["content"] == "after"
    assert updated["updated_by"] == "original-actor"
    assert updated["updated_by"] != "None"
    assert updated["metadata"] == {}


async def test_memory_path_validation_matches_sdk_contract(client):
    store = await _create_store(client)
    invalid_paths = {
        "accounts/acme": "must start with",
        "/accounts//acme": "empty segments",
        "/accounts/./acme": "must not contain",
        "/" + ("x" * 1024): "at most 1024 bytes",
        "/cafe\u0301": "NFC-normalized",
        "/accounts/\u200b": "control or format",
    }

    for path, message in invalid_paths.items():
        response = await client.post(
            f"/v1/memory_stores/{store['id']}/memories",
            headers=TEST_HEADERS,
            json={"path": path, "content": "invalid"},
        )
        assert response.status_code == 422, f"{path}: {response.text}"
        assert message in response.json()["error"]["message"]


async def test_memory_list_queries_use_store_capacity_before_pagination(client):
    store = await _create_store(client)
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    target = _memory_resource(store["id"], "accounts/acme", base_time)
    newer_matches = [
        _memory_resource(
            store["id"],
            f"accounts/other/{index}",
            base_time + timedelta(seconds=index + 1),
        )
        for index in range(1001)
    ]
    async with session_scope() as db:
        db.add(target)
        db.add_all(newer_matches)
        await db.commit()

    for query in ({"path_prefix": "/accounts"}, {"path_prefix": "/"}, {}):
        response = await client.get(
            f"/v1/memory_stores/{store['id']}/memories",
            headers=TEST_HEADERS,
            params={**query, "limit": 1000},
        )
        assert response.status_code == 200, response.text
        first_page = response.json()
        assert first_page["has_more"] is True
        assert first_page["next_page"]
        first_paths = [item["path_key"] for item in first_page["data"]]

        response = await client.get(
            f"/v1/memory_stores/{store['id']}/memories",
            headers=TEST_HEADERS,
            params={**query, "limit": 1000, "page": first_page["next_page"]},
        )
        assert response.status_code == 200, response.text
        second_page = response.json()
        second_paths = [item["path_key"] for item in second_page["data"]]
        all_paths = first_paths + second_paths
        assert len(all_paths) == 1002
        assert len(set(all_paths)) == 1002
        assert "accounts/acme" in all_paths


async def test_memory_list_depth_returns_prefix_rollups(client):
    store = await _create_store(client)
    for path in [
        "/projects/foo/notes.md",
        "/projects/foo/todo.md",
        "/projects/readme.md",
        "/accounts/acme.md",
    ]:
        response = await client.post(
            f"/v1/memory_stores/{store['id']}/memories",
            headers=TEST_HEADERS,
            json={"path": path, "content": path},
        )
        assert response.status_code == 201, response.text

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        params={"path_prefix": "/projects/", "depth": 1, "order": "asc", "view": "basic"},
    )
    assert response.status_code == 200, response.text
    items = response.json()["data"]
    assert [(item["type"], item["path"]) for item in items] == [
        ("memory_prefix", "/projects/foo/"),
        ("memory", "/projects/readme.md"),
    ]
    assert items[1]["content"] is None

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        params={"path_prefix": "/", "depth": 1, "order": "asc"},
    )
    assert response.status_code == 200, response.text
    assert [(item["type"], item["path"]) for item in response.json()["data"]] == [
        ("memory_prefix", "/accounts/"),
        ("memory_prefix", "/projects/"),
    ]


async def test_memory_version_redaction_removes_snapshot_content(client):
    store = await _create_store(client)
    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={"path": "/accounts/acme", "content": "secret preference"},
    )
    assert response.status_code == 201, response.text
    memory = response.json()

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}/versions/1",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    memory_version = response.json()
    _assert_public_memory_version(memory_version)
    assert memory_version["content"] == "secret preference"

    async with session_scope() as db:
        stored_version = await res_q.get_resource(
            db,
            resource_id=memory_version["id"],
            resource_type="memory_version",
        )
        assert stored_version is not None
        assert stored_version.data["content"] == "secret preference"
        assert stored_version.data["snapshot"]["content"] == "secret preference"

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memory_versions/{memory_version['id']}/redact",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 409

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}",
        headers=TEST_HEADERS,
        json={"content": "replacement preference"},
    )
    assert response.status_code == 200, response.text

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memory_versions/{memory_version['id']}/redact",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    redacted = response.json()
    _assert_public_memory_version(redacted)
    assert redacted["redacted"] is True
    assert redacted["content"] is None
    assert redacted["path"] is None
    assert redacted["content_sha256"] is None
    assert redacted["content_size_bytes"] is None

    async with session_scope() as db:
        stored_version = await res_q.get_resource(
            db,
            resource_id=memory_version["id"],
            resource_type="memory_version",
        )
        assert stored_version is not None
        for field in (
            "content",
            "path",
            "path_key",
            "content_sha256",
            "content_size_bytes",
        ):
            assert field not in stored_version.data
            assert field not in stored_version.data["snapshot"]

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    current_memory = response.json()
    assert current_memory["content"] == "replacement preference"


async def test_memory_delete_creates_surviving_deleted_version(client):
    store = await _create_store(client)
    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={"path": "/accounts/acme", "content": "delete me"},
    )
    assert response.status_code == 201, response.text
    memory = response.json()

    response = await client.delete(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memory_versions",
        headers=TEST_HEADERS,
        params={"memory_id": memory["id"]},
    )
    assert response.status_code == 200, response.text
    versions = response.json()["data"]
    assert [version["operation"] for version in versions] == ["deleted", "created"]
    _assert_public_memory_version(versions[0])
    assert versions[0]["content"] is None
    assert versions[0]["content_sha256"] is None
    assert versions[0]["content_size_bytes"] is None
    assert versions[0]["path"] == "/accounts/acme"

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memory_versions/{versions[0]['id']}",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["operation"] == "deleted"


async def test_memory_delete_expected_content_sha256_precondition(client):
    store = await _create_store(client)
    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={"path": "/accounts/acme", "content": "delete me"},
    )
    assert response.status_code == 201, response.text
    memory = response.json()

    response = await client.delete(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}",
        headers=TEST_HEADERS,
        params={"expected_content_sha256": "stale"},
    )
    assert response.status_code == 409

    response = await client.delete(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}",
        headers=TEST_HEADERS,
        params={"expected_content_sha256": memory["content_sha256"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["type"] == "memory_deleted"


async def test_memory_version_list_rejects_unknown_operation(client):
    store = await _create_store(client)

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memory_versions",
        headers=TEST_HEADERS,
        params={"operation": "renamed"},
    )

    assert response.status_code == 422
    assert "operation" in response.json()["error"]["message"]


async def test_memory_version_list_filters_api_key_session_and_view(client):
    store = await _create_store(client)
    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={
            "path": "/accounts/acme",
            "content": "created by key a",
            "actor": "key-a",
            "session_id": "sess_a",
        },
    )
    assert response.status_code == 201, response.text
    memory = response.json()

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}",
        headers=TEST_HEADERS,
        json={
            "content": "updated by key b",
            "actor": "key-b",
            "session_id": "sess_b",
        },
    )
    assert response.status_code == 200, response.text

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memory_versions",
        headers=TEST_HEADERS,
        params={"api_key_id": "key-a", "view": "basic"},
    )
    assert response.status_code == 200, response.text
    versions = response.json()["data"]
    assert [version["created_by"]["api_key_id"] for version in versions] == ["key-a"]
    _assert_public_memory_version(versions[0])
    assert versions[0]["content"] is None
    assert "session_id" not in versions[0]

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memory_versions",
        headers=TEST_HEADERS,
        params={"session_id": "sess_b", "view": "full"},
    )
    assert response.status_code == 200, response.text
    versions = response.json()["data"]
    assert [version["created_by"]["api_key_id"] for version in versions] == ["key-b"]
    _assert_public_memory_version(versions[0])
    assert versions[0]["content"] == "updated by key b"
    assert "session_id" not in versions[0]

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memory_versions/{versions[0]['id']}",
        headers=TEST_HEADERS,
        params={"view": "basic"},
    )
    assert response.status_code == 200, response.text
    _assert_public_memory_version(response.json())
    assert response.json()["content"] is None


async def test_memory_store_write_limits(client):
    store = await _create_store(client)

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={"path": "/too-large", "content": "x" * (100 * 1024 + 1)},
    )
    assert response.status_code == 413

    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with session_scope() as db:
        db.add_all(
            _memory_resource(store["id"], f"item/{index}", base_time + timedelta(seconds=index))
            for index in range(2000)
        )
        await db.commit()

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={"path": "/overflow", "content": "overflow"},
    )
    assert response.status_code == 409


async def test_archived_memory_store_is_read_only_and_not_attachable(client):
    store = await _create_store(client)
    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={"path": "/accounts/acme", "content": "read only"},
    )
    assert response.status_code == 201, response.text
    memory = response.json()

    response = await client.post(f"/v1/memory_stores/{store['id']}/archive", headers=TEST_HEADERS)
    assert response.status_code == 200, response.text

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={"path": "/accounts/new", "content": "blocked"},
    )
    assert response.status_code == 409

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}",
        headers=TEST_HEADERS,
        json={"content": "also blocked"},
    )
    assert response.status_code == 409

    response = await client.get(
        f"/v1/memory_stores/{store['id']}/memories/{memory['id']}",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["content"] == "read only"

    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Memory Attach Agent", "model": {"id": "gpt-5.5"}},
    )
    assert response.status_code == 201, response.text
    agent = response.json()

    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "memory-attach-env", "config": {"type": "cloud"}},
    )
    assert response.status_code == 201, response.text
    environment = response.json()

    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"type": "agent", "id": agent["id"], "version": agent["version"]},
            "environment_id": environment["id"],
            "resources": [{"type": "memory_store", "memory_store_id": store["id"]}],
        },
    )
    assert response.status_code == 404


async def test_memory_version_retrieve_requires_matching_store(client):
    store = await _create_store(client)
    other_store = await _create_store(client)

    response = await client.post(
        f"/v1/memory_stores/{store['id']}/memories",
        headers=TEST_HEADERS,
        json={"path": "/accounts/acme", "content": "store scoped"},
    )
    assert response.status_code == 201, response.text
    memory = response.json()

    response = await client.get(
        f"/v1/memory_stores/{other_store['id']}/memory_versions/{memory['memory_version_id']}",
        headers=TEST_HEADERS,
    )
    assert response.status_code == 404


async def test_memory_version_lists_page_past_one_thousand_rows(client):
    store = await _create_store(client)
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    memory = _memory_resource(store["id"], "accounts/acme", base_time)
    versions = [
        _memory_version_resource(
            store["id"],
            memory.id,
            version,
            base_time + timedelta(seconds=version),
        )
        for version in range(1, 1003)
    ]
    async with session_scope() as db:
        db.add(memory)
        db.add_all(versions)
        await db.commit()

    paths = (
        f"/v1/memory_stores/{store['id']}/memories/{memory.id}/versions",
        f"/v1/memory_stores/{store['id']}/memory_versions",
    )
    for path in paths:
        response = await client.get(
            path,
            headers=TEST_HEADERS,
            params={"limit": 1000},
        )
        assert response.status_code == 200, response.text
        first_page = response.json()
        assert first_page["has_more"] is True
        assert first_page["next_page"]

        response = await client.get(
            path,
            headers=TEST_HEADERS,
            params={"limit": 1000, "page": first_page["next_page"]},
        )
        assert response.status_code == 200, response.text
        second_page = response.json()
        assert second_page["has_more"] is False
        assert second_page["next_page"] is None

        all_versions = first_page["data"] + second_page["data"]
        assert len(all_versions) == 1002
        assert len({item["id"] for item in all_versions}) == 1002
        assert [item["version"] for item in all_versions] == list(
            range(1002, 0, -1)
        )


def _memory_resource(memory_store_id: str, path_key: str, created_at: datetime) -> ManagedResource:
    return ManagedResource(
        id=new_id("mem"),
        organization_id=TEST_ORGANIZATION_ID,
        resource_type="memory",
        parent_id=memory_store_id,
        name=path_key,
        data={
            "path": f"/{path_key}",
            "path_key": path_key,
            "content": "remembered",
            "version": 1,
            "metadata": {},
            "redacted": False,
            "memory_version_id": "",
        },
        created_at=created_at,
        updated_at=created_at,
    )


def _memory_version_resource(
    memory_store_id: str,
    memory_id: str,
    version: int,
    created_at: datetime,
) -> ManagedResource:
    content = f"version {version}"
    return ManagedResource(
        id=new_id("memver"),
        organization_id=TEST_ORGANIZATION_ID,
        resource_type="memory_version",
        parent_id=memory_id,
        version=version,
        data={
            "memory_store_id": memory_store_id,
            "memory_id": memory_id,
            "memory_version": version,
            "path": "/accounts/acme",
            "content": content,
            "content_sha256": f"{version:064x}",
            "content_size_bytes": len(content.encode("utf-8")),
            "actor": "pagination-test",
            "created_by": {
                "type": "api_actor",
                "api_key_id": "pagination-test",
            },
            "operation": "created" if version == 1 else "modified",
            "redacted": False,
        },
        created_at=created_at,
        updated_at=created_at,
    )
