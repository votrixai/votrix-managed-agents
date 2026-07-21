from __future__ import annotations

import json

import httpx
import pytest

from votrix import (
    AsyncVotrix,
    Memory,
    MemoryListItem,
    MemoryPrecondition,
    MemoryStore,
    MemoryVersion,
    SessionFundingRequest,
    UsageEntry,
    UsagePage,
)


def make_client(handler):
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk = AsyncVotrix(
        api_key="vma_test_resources",
        base_url="https://vma.test",
        max_retries=0,
        http_client=http_client,
    )
    return sdk, http_client


def agent_payload(agent_id: str) -> dict:
    return {
        "id": agent_id,
        "type": "agent",
        "name": agent_id,
        "version": 1,
        "model": {"id": "deepseek/deepseek-v4-pro", "provider": "openrouter"},
        "future_agent_field": {"enabled": True},
    }


@pytest.mark.asyncio
async def test_lazy_pagination_and_nested_forward_compatible_models():
    calls: list[str | None] = []

    def handler(request: httpx.Request):
        page = request.url.params.get("page")
        calls.append(page)
        if page is None:
            return httpx.Response(
                200,
                json={
                    "data": [agent_payload("agent_1")],
                    "has_more": True,
                    "first_id": "agent_1",
                    "last_id": "agent_1",
                    "next_page": "page_next",
                },
            )
        assert page == "page_next"
        return httpx.Response(
            200,
            json={
                "data": [agent_payload("agent_2")],
                "has_more": False,
                "first_id": "agent_2",
                "last_id": "agent_2",
            },
        )

    sdk, http_client = make_client(handler)
    paginator = sdk.agents.list(limit=1)
    assert calls == []
    page = await paginator
    assert page.data[0].model.id == "deepseek/deepseek-v4-pro"
    assert page.data[0].future_agent_field == {"enabled": True}
    assert (await page.get_next_page()).data[0].id == "agent_2"

    calls.clear()
    assert [agent.id async for agent in sdk.agents.list(limit=1)] == ["agent_1", "agent_2"]
    assert calls == [None, "page_next"]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_provider_catalog_hides_internal_secret_mapping():
    def handler(request: httpx.Request):
        assert request.url.path == "/v1/model_providers"
        assert not request.url.query
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "openrouter",
                        "type": "model_provider",
                        "display_name": "OpenRouter",
                        "adapter": "openai",
                        "credential_type": "api_key",
                        "default_model": "deepseek/deepseek-v4-pro",
                        "capabilities": {
                            "streaming": True,
                            "tool_calls": True,
                            "multimodal_input": True,
                            "reasoning": True,
                            "native_structured_output": False,
                            "future_capability": True,
                        },
                    }
                ],
                "has_more": False,
            },
        )

    sdk, http_client = make_client(handler)
    page = await sdk.model_providers.list()
    provider = page.data[0]
    assert provider.capabilities.streaming is True
    assert provider.capabilities.future_capability is True
    dumped = provider.model_dump()
    assert "api_key_env" not in dumped
    assert "secret_name" not in dumped
    await http_client.aclose()


@pytest.mark.asyncio
async def test_usage_list_serializes_filters_and_parses_opaque_pages():
    queries: list[dict[str, str]] = []

    def usage_payload(entry_id: str, quantity: int) -> dict:
        return {
            "id": entry_id,
            "type": "usage",
            "organization_id": "org_sdk",
            "metric": "model_tokens",
            "quantity": quantity,
            "unit": "token",
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "source_type": "session",
            "source_id": "sess_sdk",
            "dimensions": {"input_tokens": quantity},
            "data": {"accounting_phase": "postflight_actual"},
            "occurred_at": "2026-07-16T14:00:00Z",
            "future_usage_field": "preserved",
        }

    def handler(request: httpx.Request):
        assert request.url.path == "/v1/usage"
        query = dict(request.url.params)
        queries.append(query)
        if query.get("page") is None:
            return httpx.Response(
                200,
                json={
                    "data": [usage_payload("usage_2", 20)],
                    "has_more": True,
                    "first_id": "usage_2",
                    "last_id": "usage_2",
                    "next_page": "usage_opaque_next",
                },
            )
        assert query["page"] == "usage_opaque_next"
        return httpx.Response(
            200,
            json={
                "data": [usage_payload("usage_1", 10)],
                "has_more": False,
                "first_id": "usage_1",
                "last_id": "usage_1",
                "next_page": None,
            },
        )

    sdk, http_client = make_client(handler)
    paginator = sdk.usage.list(
        limit=1,
        session_id="sess_sdk",
        metric="model_tokens",
        occurred_at_gt="2026-07-16T10:00:00Z",
        occurred_at_gte="2026-07-16T11:00:00Z",
        occurred_at_lt="2026-07-17T00:00:00Z",
        occurred_at_lte="2026-07-17T01:00:00Z",
    )
    page: UsagePage = await paginator
    assert isinstance(page.data[0], UsageEntry)
    assert page.data[0].quantity == 20
    assert page.data[0].future_usage_field == "preserved"

    next_page = await page.get_next_page()
    assert next_page is not None
    assert next_page.data[0].id == "usage_1"
    assert next_page.data[0].occurred_at.isoformat() == "2026-07-16T14:00:00+00:00"
    assert await next_page.get_next_page() is None
    assert queries == [
        {
            "limit": "1",
            "session_id": "sess_sdk",
            "metric": "model_tokens",
            "occurred_at[gt]": "2026-07-16T10:00:00Z",
            "occurred_at[gte]": "2026-07-16T11:00:00Z",
            "occurred_at[lt]": "2026-07-17T00:00:00Z",
            "occurred_at[lte]": "2026-07-17T01:00:00Z",
        },
        {
            "limit": "1",
            "session_id": "sess_sdk",
            "metric": "model_tokens",
            "occurred_at[gt]": "2026-07-16T10:00:00Z",
            "occurred_at[gte]": "2026-07-16T11:00:00Z",
            "occurred_at[lt]": "2026-07-17T00:00:00Z",
            "occurred_at[lte]": "2026-07-17T01:00:00Z",
            "page": "usage_opaque_next",
        },
    ]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_native_model_credential_lifecycle_never_exposes_secret_shape():
    calls: list[tuple[str, str, dict[str, str], dict]] = []

    def payload(*, archived: bool = False) -> dict:
        return {
            "id": "credential_1",
            "type": "model_credential",
            "vault_id": "vault_1",
            "model_provider": "openrouter",
            "display_name": "End-user key",
            "metadata": {},
            "archived_at": "2026-07-15T00:00:00Z" if archived else None,
        }

    def handler(request: httpx.Request):
        body = json.loads(request.content) if request.content else {}
        calls.append((request.method, request.url.path, dict(request.url.params), body))
        if request.method == "GET" and request.url.path.endswith("/model_credentials"):
            return httpx.Response(200, json={"data": [payload()], "has_more": False})
        if request.method == "GET":
            return httpx.Response(200, json=payload())
        if request.method == "DELETE":
            return httpx.Response(
                200,
                json={
                    "id": "credential_1",
                    "type": "model_credential_deleted",
                    "deleted": True,
                },
            )
        if request.url.path.endswith("/archive"):
            return httpx.Response(200, json=payload(archived=True))
        return httpx.Response(201 if len(calls) == 1 else 200, json=payload())

    sdk, http_client = make_client(handler)
    created = await sdk.vaults.model_credentials.create(
        "vault_1", provider="openrouter", api_key="sk-user", display_name="End-user key"
    )
    rotated = await sdk.vaults.model_credentials.rotate(
        "vault_1", created.id, api_key="sk-rotated"
    )
    page = await sdk.vaults.model_credentials.list(
        "vault_1",
        include_archived=True,
    )
    retrieved = await sdk.vaults.model_credentials.retrieve(
        created.id,
        vault_id="vault_1",
    )
    archived = await sdk.vaults.model_credentials.archive(
        created.id,
        vault_id="vault_1",
    )
    deleted = await sdk.vaults.model_credentials.delete(
        created.id,
        vault_id="vault_1",
    )
    assert created.model_provider == rotated.model_provider == "openrouter"
    assert page.data[0].id == retrieved.id == archived.id == created.id
    assert archived.archived_at is not None
    assert deleted.type == "model_credential_deleted"
    assert calls == [
        (
            "POST",
            "/v1/vaults/vault_1/model_credentials",
            {},
            {"provider": "openrouter", "api_key": "sk-user", "display_name": "End-user key"},
        ),
        (
            "POST",
            "/v1/vaults/vault_1/model_credentials/credential_1",
            {},
            {"api_key": "sk-rotated"},
        ),
        (
            "GET",
            "/v1/vaults/vault_1/model_credentials",
            {"limit": "50", "include_archived": "true"},
            {},
        ),
        ("GET", "/v1/vaults/vault_1/model_credentials/credential_1", {}, {}),
        (
            "POST",
            "/v1/vaults/vault_1/model_credentials/credential_1/archive",
            {},
            {},
        ),
        ("DELETE", "/v1/vaults/vault_1/model_credentials/credential_1", {}, {}),
    ]
    public_values = [created, rotated, *page.data, retrieved, archived, deleted]
    assert "secret_name" not in " ".join(item.model_dump_json() for item in public_values)
    assert "sk-user" not in " ".join(item.model_dump_json() for item in public_values)
    assert "sk-rotated" not in " ".join(item.model_dump_json() for item in public_values)
    await http_client.aclose()


@pytest.mark.asyncio
async def test_api_key_lifecycle_exposes_secrets_only_once():
    calls: list[tuple[str, str, dict[str, str], dict]] = []

    def payload(key_id: str, *, secret: str | None = None, revoked: bool = False) -> dict:
        value = {
            "id": key_id,
            "type": "api_key",
            "organization_id": "org_test",
            "name": "Production",
            "prefix": "vma_test_key",
            "scopes": ["api", "api_keys:manage"],
            "expires_at": None,
            "created_by": "key_admin",
            "metadata": {"owner": "platform"},
            "last_used_at": None,
            "revoked_at": "2026-07-15T00:00:00Z" if revoked else None,
            "revoked_by": "key_admin" if revoked else None,
            "revocation_reason": "rollover" if revoked else None,
            "replaced_by_key_id": None,
            "replaces_key_id": "key_1" if key_id == "key_2" else None,
            "created_at": "2026-07-15T00:00:00Z",
            "updated_at": "2026-07-15T00:00:00Z",
        }
        if secret is not None:
            value["secret"] = secret
        return value

    def handler(request: httpx.Request):
        body = json.loads(request.content) if request.content else {}
        calls.append((request.method, request.url.path, dict(request.url.params), body))
        if request.method == "POST" and request.url.path == "/v1/api_keys":
            return httpx.Response(201, json=payload("key_1", secret="vma_test_create_secret"))
        if request.url.path.endswith("/rotate"):
            return httpx.Response(201, json=payload("key_2", secret="vma_test_rotate_secret"))
        if request.url.path.endswith("/revoke"):
            # A defensive extra field must never leak through the safe metadata model.
            return httpx.Response(200, json=payload("key_2", secret="must_be_ignored", revoked=True))
        if request.method == "GET" and request.url.path == "/v1/api_keys":
            return httpx.Response(
                200,
                json={"data": [payload("key_1", secret="must_be_ignored")], "has_more": False},
            )
        return httpx.Response(200, json=payload("key_1", secret="must_be_ignored"))

    sdk, http_client = make_client(handler)
    created = await sdk.api_keys.create(
        name="Production",
        scopes=["api", "api_keys:manage"],
        metadata={"owner": "platform"},
    )
    page = await sdk.api_keys.list(include_revoked=False)
    retrieved = await sdk.api_keys.retrieve(created.id)
    rotated = await sdk.api_keys.rotate(created.id, reason="rollover")
    revoked = await sdk.api_keys.revoke(rotated.id, reason="rollover")

    assert created.secret.get_secret_value() == "vma_test_create_secret"
    assert rotated.secret.get_secret_value() == "vma_test_rotate_secret"
    assert "vma_test_create_secret" not in repr(created.secret)
    assert "vma_test_rotate_secret" not in repr(rotated.secret)
    for safe in [*page.data, retrieved, revoked]:
        assert "secret" not in safe.model_dump()
    assert calls == [
        (
            "POST",
            "/v1/api_keys",
            {},
            {
                "name": "Production",
                "scopes": ["api", "api_keys:manage"],
                "metadata": {"owner": "platform"},
            },
        ),
        ("GET", "/v1/api_keys", {"limit": "50", "include_revoked": "false"}, {}),
        ("GET", "/v1/api_keys/key_1", {}, {}),
        ("POST", "/v1/api_keys/key_1/rotate", {}, {"reason": "rollover"}),
        ("POST", "/v1/api_keys/key_2/revoke", {}, {"reason": "rollover"}),
    ]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_nested_public_resource_argument_order():
    paths: list[str] = []

    def handler(request: httpx.Request):
        paths.append(request.url.path)
        path = request.url.path
        if "/sessions/" in path:
            return httpx.Response(200, json={"id": "resource_1", "type": "file", "file_id": "file_1"})
        if "/skills/" in path:
            return httpx.Response(
                200,
                json={
                    "id": "skill_version_2",
                    "type": "skill_version",
                    "skill_id": "skill_1",
                    "version": "2",
                },
            )
        raise AssertionError(f"unexpected request: {request.url.path}")

    sdk, http_client = make_client(handler)
    await sdk.sessions.resources.retrieve("resource_1", session_id="session_1")
    await sdk.skills.versions.retrieve(2, skill_id="skill_1")
    assert paths == [
        "/v1/sessions/session_1/resources/resource_1",
        "/v1/skills/skill_1/versions/2",
    ]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_skill_files_use_repeated_files_multipart_field():
    bodies: list[bytes] = []

    async def handler(request: httpx.Request):
        body = await request.aread()
        bodies.append(body)
        return httpx.Response(
            201,
            json={
                "id": "skill_1",
                "type": "skill",
                "display_title": "Contract skill",
                "version": {
                    "id": "skill_version_1",
                    "type": "skill_version",
                    "skill_id": "skill_1",
                    "version": "1",
                },
            },
        )

    sdk, http_client = make_client(handler)
    await sdk.skills.create(
        display_title="Contract skill",
        files=[("skill/SKILL.md", b"---\nname: contract\n---\n", "text/markdown")],
    )
    await sdk.skills.create(display_title="Archive", archive=b"PK fake")
    assert b'name="files"; filename="skill/SKILL.md"' in bodies[0]
    assert b'name="files"; filename="skill.zip"' in bodies[1]
    assert b'form-data; name="skill.zip"' not in bodies[1]
    with pytest.raises(ValueError, match="requires archive or files"):
        await sdk.skills.create(display_title="Empty")
    await http_client.aclose()


@pytest.mark.asyncio
async def test_file_download_read_iter_and_write(tmp_path):
    def handler(_request: httpx.Request):
        return httpx.Response(
            200,
            content=b"abcdef",
            headers={
                "content-type": "application/octet-stream",
                "content-disposition": 'attachment; filename="result.bin"',
            },
        )

    sdk, http_client = make_client(handler)
    download = await sdk.files.download("file_1")
    assert list(download.iter_bytes(2)) == [b"ab", b"cd", b"ef"]
    assert await download.read() == b"abcdef"
    destination = tmp_path / "result.bin"
    assert await download.write_to_file(destination) == destination
    assert destination.read_bytes() == b"abcdef"
    assert download.filename == "result.bin"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_file_download_is_lazy_and_streams_async_chunks():
    class TrackingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.started = False
            self.closed = False

        async def __aiter__(self):
            self.started = True
            yield b"abc"
            yield b"def"

        async def aclose(self) -> None:
            self.closed = True

    source = TrackingStream()

    def handler(_request: httpx.Request):
        return httpx.Response(
            200,
            stream=source,
            headers={"content-type": "application/octet-stream"},
        )

    sdk, http_client = make_client(handler)
    download = await sdk.files.download("file_1", stream=True)
    assert source.started is False
    chunks = [chunk async for chunk in download.aiter_bytes(chunk_size=2)]
    assert chunks == [b"ab", b"cd", b"ef"]
    assert source.closed is True
    await http_client.aclose()


@pytest.mark.asyncio
async def test_before_id_pagination_keeps_direction_and_drops_original_boundary():
    queries: list[dict[str, str]] = []

    def file(file_id: str) -> dict:
        return {"id": file_id, "type": "file"}

    def handler(request: httpx.Request):
        query = dict(request.url.params)
        queries.append(query)
        boundary = query.get("before_id")
        assert "after_id" not in query
        if boundary == "file_5":
            data = [file("file_3"), file("file_4")]
        else:
            data = [file("file_1"), file("file_2")]
        return httpx.Response(
            200,
            json={
                "data": data,
                "has_more": True,
                "first_id": data[0]["id"],
                "last_id": data[-1]["id"],
            },
        )

    sdk, http_client = make_client(handler)
    ids = [item.id async for item in sdk.files.list(limit=2, before_id="file_5")]
    assert ids == ["file_3", "file_4", "file_1", "file_2"]
    assert [query["before_id"] for query in queries] == ["file_5", "file_3", "file_1"]
    assert all(query["before_id"] != "file_5" for query in queries[1:])
    await http_client.aclose()


@pytest.mark.asyncio
async def test_before_id_pagination_deduplicates_an_inclusive_boundary():
    queries: list[str] = []

    def handler(request: httpx.Request):
        boundary = request.url.params["before_id"]
        queries.append(boundary)
        if boundary == "file_5":
            ids = ["file_3", "file_4"]
        else:
            # A defensive compatibility case for servers that repeat the
            # boundary item in the following page.
            ids = ["file_3", "file_2"]
        return httpx.Response(
            200,
            json={
                "data": [{"id": item_id, "type": "file"} for item_id in ids],
                "has_more": True,
                "first_id": ids[0],
                "last_id": ids[-1],
            },
        )

    sdk, http_client = make_client(handler)
    ids = [item.id async for item in sdk.files.list(limit=2, before_id="file_5")]
    assert ids == ["file_3", "file_4", "file_2"]
    assert queries == ["file_5", "file_3"]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_list_date_filters_use_server_bracket_names():
    queries: list[dict[str, str]] = []

    def handler(request: httpx.Request):
        queries.append(dict(request.url.params))
        return httpx.Response(200, json={"data": [], "has_more": False})

    sdk, http_client = make_client(handler)
    await sdk.sessions.list(created_at_gte="2026-01-01T00:00:00Z")
    await sdk.sessions.events.list("session_1", created_at_lt="2026-02-01T00:00:00Z")
    assert queries[0]["created_at[gte]"] == "2026-01-01T00:00:00Z"
    assert queries[1]["created_at[lt]"] == "2026-02-01T00:00:00Z"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_memory_store_resource_lifecycle_and_nested_filters():
    calls: list[tuple[str, str, dict[str, str], dict]] = []
    raw_paths: list[str] = []
    timestamp = "2026-07-20T12:00:00Z"

    def store_payload(*, archived: bool = False) -> dict:
        return {
            "id": "memstore_1",
            "type": "memory_store",
            "name": "Account context",
            "description": "Support memories",
            "metadata": {"team": "support"},
            "archived_at": timestamp if archived else None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def memory_payload() -> dict:
        return {
            "id": "mem_1",
            "type": "memory",
            "memory_store_id": "memstore_1",
            "memory_version_id": "memver_1",
            "path": "/accounts/acme.md",
            "path_key": "accounts/acme.md",
            "content": "ACME prefers email.",
            "content_sha256": "sha256_1",
            "content_size_bytes": 19,
            "version": 1,
            "metadata": {"tier": "enterprise"},
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def version_payload(*, redacted: bool = False) -> dict:
        return {
            "id": "memver_1",
            "type": "memory_version",
            "memory_store_id": "memstore_1",
            "memory_id": "mem_1",
            "operation": "created",
            "version": 1,
            "path": None if redacted else "/accounts/acme.md",
            "content": None if redacted else "ACME prefers email.",
            "content_sha256": None if redacted else "sha256_1",
            "content_size_bytes": None if redacted else 19,
            "redacted_at": timestamp if redacted else None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def handler(request: httpx.Request):
        body = json.loads(request.content) if request.content else {}
        query = dict(request.url.params)
        calls.append((request.method, request.url.path, query, body))
        raw_paths.append(request.url.raw_path.decode())
        path = request.url.path
        if request.method == "DELETE":
            deleted_type = "memory_deleted" if "/memories/" in path else "memory_store_deleted"
            deleted_id = "mem_1" if "/memories/" in path else "memstore_1"
            return httpx.Response(200, json={"id": deleted_id, "type": deleted_type, "deleted": True})
        if path.endswith("/redact"):
            return httpx.Response(200, json=version_payload(redacted=True))
        if "/memory_versions/" in path:
            return httpx.Response(200, json=version_payload())
        if path.endswith("/memory_versions"):
            return httpx.Response(200, json={"data": [version_payload()], "has_more": False})
        if "/memories/" in path and path.endswith("/versions"):
            return httpx.Response(200, json={"data": [version_payload()], "has_more": False})
        if "/memories/" in path and "/versions/" in path:
            return httpx.Response(200, json=version_payload())
        if path.endswith("/memories/by_path") or "/memories/" in path:
            return httpx.Response(200, json=memory_payload())
        if path.endswith("/memories"):
            if request.method == "GET":
                data = (
                    [{"type": "memory_prefix", "path": "/accounts/"}]
                    if "depth" in query
                    else [memory_payload()]
                )
                return httpx.Response(200, json={"data": data, "has_more": False})
            return httpx.Response(201, json=memory_payload())
        if request.method == "GET" and path == "/v1/memory_stores":
            return httpx.Response(200, json={"data": [store_payload()], "has_more": False})
        return httpx.Response(
            201 if request.method == "POST" and path == "/v1/memory_stores" else 200,
            json=store_payload(archived=path.endswith("/archive")),
        )

    sdk, http_client = make_client(handler)
    store = await sdk.memory_stores.create(
        name="Account context",
        description="Support memories",
        metadata={"team": "support"},
    )
    assert isinstance(store, MemoryStore)
    assert (await sdk.memory_stores.retrieve(store.id)).id == store.id
    await sdk.memory_stores.update(store.id, description=None, metadata={"old": None})
    stores = await sdk.memory_stores.list(
        include_archived=True,
        created_at_gte="2026-07-01T00:00:00Z",
    )
    assert isinstance(stores.data[0], MemoryStore)

    memory = await sdk.memory_stores.memories.create(
        store.id,
        path=["accounts", "acme.md"],
        content="ACME prefers email.",
        metadata={"tier": "enterprise"},
        actor="key_1",
        session_id="sess_1",
        view="full",
    )
    assert isinstance(memory, Memory)
    assert (await sdk.memory_stores.memories.retrieve(
        memory.id, memory_store_id=store.id, view="full"
    )).id == memory.id
    assert (await sdk.memory_stores.memories.by_path(
        "/accounts/acme.md", memory_store_id=store.id, view="basic"
    )).id == memory.id
    await sdk.memory_stores.memories.update(
        memory.id,
        memory_store_id=store.id,
        content="ACME prefers chat.",
        precondition=MemoryPrecondition(content_sha256="sha256_1"),
        if_version=1,
        view="full",
    )
    memories = await sdk.memory_stores.memories.list(
        store.id,
        path_prefix="/accounts/",
        view="full",
        order="desc",
    )
    assert isinstance(memories.data[0], Memory)
    prefixes = await sdk.memory_stores.memories.list(store.id, depth=1)
    assert isinstance(prefixes.data[0], MemoryListItem)
    assert prefixes.data[0].type == "memory_prefix"

    history = await sdk.memory_stores.memories.versions.list(
        "memory/one",
        memory_store_id="store/one",
        limit=10,
    )
    assert isinstance(history.data[0], MemoryVersion)
    history_version = await sdk.memory_stores.memories.versions.retrieve(
        2,
        memory_store_id="store/one",
        memory_id="memory/one",
    )
    assert history_version.id == "memver_1"

    versions = await sdk.memory_stores.memory_versions.list(
        store.id,
        memory_id=memory.id,
        operation="created",
        api_key_id="key_1",
        session_id="sess_1",
        view="full",
        created_at_lte="2026-08-01T00:00:00Z",
    )
    assert isinstance(versions.data[0], MemoryVersion)
    version = await sdk.memory_stores.memory_versions.retrieve(
        versions.data[0].id,
        memory_store_id=store.id,
        view="full",
    )
    redacted = await sdk.memory_stores.memory_versions.redact(
        version.id,
        memory_store_id=store.id,
    )
    assert redacted.redacted_at is not None

    deleted_memory = await sdk.memory_stores.memories.delete(
        memory.id,
        memory_store_id=store.id,
        expected_content_sha256="sha256_1",
    )
    assert deleted_memory.type == "memory_deleted"
    assert (await sdk.memory_stores.archive(store.id)).archived_at is not None
    assert (await sdk.memory_stores.delete(store.id)).type == "memory_store_deleted"

    create_memory_call = next(call for call in calls if call[1].endswith("/memories") and call[0] == "POST")
    assert create_memory_call[2] == {"view": "full"}
    assert create_memory_call[3] == {
        "path": ["accounts", "acme.md"],
        "content": "ACME prefers email.",
        "metadata": {"tier": "enterprise"},
        "actor": "key_1",
        "session_id": "sess_1",
    }
    update_memory_call = next(call for call in calls if call[1].endswith("/memories/mem_1") and call[0] == "POST")
    assert update_memory_call[3]["precondition"] == {
        "type": "content_sha256",
        "content_sha256": "sha256_1",
    }
    assert any(
        call[1].endswith("/memories/by_path")
        and call[2] == {"path": "/accounts/acme.md", "view": "basic"}
        for call in calls
    )
    assert any(
        call[1].endswith("/memory_versions")
        and call[2]["created_at[lte]"] == "2026-08-01T00:00:00Z"
        for call in calls
    )
    assert any(
        call[1] == "/v1/memory_stores/store/one/memories/memory/one/versions"
        and call[2] == {"limit": "10"}
        for call in calls
    )
    assert any(
        call[1] == "/v1/memory_stores/store/one/memories/memory/one/versions/2"
        for call in calls
    )
    assert any(
        path.startswith(
            "/v1/memory_stores/store%2Fone/memories/memory%2Fone/versions?"
        )
        for path in raw_paths
    )
    assert (
        "/v1/memory_stores/store%2Fone/memories/memory%2Fone/versions/2"
        in raw_paths
    )
    await http_client.aclose()


@pytest.mark.asyncio
async def test_public_client_surface_excludes_deferred_resources():
    sdk, http_client = make_client(lambda _request: httpx.Response(500))
    assert {
        "api_keys",
        "agents",
        "environments",
        "sessions",
        "files",
        "memory_stores",
        "skills",
        "usage",
        "vaults",
        "model_providers",
    } <= set(vars(sdk))
    assert not hasattr(sdk, "deployments")
    assert not hasattr(sdk, "user_profiles")
    assert not hasattr(sdk.vaults, "credentials")
    assert hasattr(sdk.vaults, "model_credentials")
    await http_client.aclose()


@pytest.mark.asyncio
async def test_session_create_accepts_explicit_idempotency_key():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request):
        seen["key"] = request.headers["idempotency-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "session_1", "type": "session"})

    sdk, http_client = make_client(handler)
    session = await sdk.sessions.create(
        agent="agent_1",
        environment_id="environment_1",
        idempotency_key="create-session-42",
    )
    assert session.id == "session_1"
    assert seen == {
        "key": "create-session-42",
        "body": {"agent": "agent_1", "environment_id": "environment_1"},
    }
    await http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "funding_type",
    ["organization_default", "byok", "platform_credits"],
)
async def test_session_create_serializes_native_funding_request(funding_type: str):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "session_1", "type": "session"})

    sdk, http_client = make_client(handler)
    session = await sdk.sessions.create(
        agent="agent_1",
        environment_id="environment_1",
        funding=SessionFundingRequest(type=funding_type),
    )

    assert session.id == "session_1"
    assert seen["body"] == {
        "agent": "agent_1",
        "environment_id": "environment_1",
        "funding": {"type": funding_type},
    }
    await http_client.aclose()


@pytest.mark.asyncio
async def test_skill_rejects_mixed_json_and_multipart_files():
    sdk, http_client = make_client(lambda _request: httpx.Response(500))
    with pytest.raises(ValueError, match="all JSON file objects or all multipart tuples"):
        await sdk.skills.create(
            files=[
                {"filename": "skill/SKILL.md", "content": "hello"},
                ("skill/reference.txt", b"reference", "text/plain"),
            ]
        )
    await http_client.aclose()


@pytest.mark.asyncio
async def test_session_nested_agent_and_resource_models():
    def handler(_request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "id": "session_1",
                "type": "session",
                "agent": agent_payload("agent_1"),
                "agent_id": "agent_1",
                "agent_version": 1,
                "environment_id": "environment_1",
                "status": "idle",
                "resources": [{"id": "resource_1", "type": "file", "file_id": "file_1"}],
            },
        )

    sdk, http_client = make_client(handler)
    session = await sdk.sessions.retrieve("session_1")
    assert session.agent.id == "agent_1"
    assert session.agent.model.provider == "openrouter"
    assert session.resources[0].file_id == "file_1"
    await http_client.aclose()
