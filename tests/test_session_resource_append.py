from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import storage
from app.config import get_settings
from app.db.engine import session_scope
from app.db.queries import resources as res_q
from app.db.queries import session_sandboxes as sandboxes_q
from app.db.queries import sessions as sessions_q
from tests.conftest import TEST_HEADERS
from tests.test_sandbox_lifecycle import FakeLifecycleProvider, _configure


ORGANIZATION_ID = "org_test"
UPLOAD_ROOT = "/mnt/session/uploads"


async def _create_e2b_session(
    client,
    monkeypatch,
    *,
    skills: list[dict] | None = None,
    resources: list[dict] | None = None,
):
    import app.runtime.sandbox_lifecycle as lifecycle

    _configure(monkeypatch)
    provider = FakeLifecycleProvider()
    monkeypatch.setattr(lifecycle, "build_e2b_provider", lambda: provider)

    agent_payload = {"name": "Append security agent", "model": {"id": "gpt-5.5"}}
    if skills is not None:
        agent_payload["skills"] = skills
    response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json=agent_payload,
    )
    assert response.status_code == 201, response.text
    agent = response.json()

    response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={
            "name": "Append security environment",
            "config": {"type": "cloud", "networking": {"type": "none"}},
        },
    )
    assert response.status_code == 201, response.text
    environment = response.json()

    session_payload = {"agent": agent["id"], "environment_id": environment["id"]}
    if resources is not None:
        session_payload["resources"] = resources
    response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json=session_payload,
    )
    assert response.status_code == 201, response.text
    return response.json(), provider


async def _upload(client, filename: str, content: bytes):
    response = await client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": (filename, content, "text/plain")},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _add(client, session_id: str, file_id: str, mount_path: str):
    return await client.post(
        f"/v1/sessions/{session_id}/resources",
        headers=TEST_HEADERS,
        json={"type": "file", "file_id": file_id, "mount_path": mount_path},
    )


async def _set_session_state(
    session_id: str,
    *,
    status: str = "idle",
    stop_reason: dict | None = None,
    archived: bool = False,
) -> None:
    async with session_scope() as db:
        session = await sessions_q.get_session(
            db,
            session_id,
            organization_id=ORGANIZATION_ID,
            for_update=True,
        )
        assert session is not None
        session.status = status
        session.stop_reason = stop_reason
        session.archived_at = datetime.now(timezone.utc) if archived else None
        await db.commit()


@pytest.mark.parametrize(
    "mount_path",
    [
        "/workspace/outside.txt",
        "/mnt/session/other.txt",
        f"{UPLOAD_ROOT}/nested/input.txt",
    ],
)
async def test_e2b_append_rejects_paths_outside_direct_upload_root_before_provider_touch(
    client,
    monkeypatch,
    mount_path,
):
    session, provider = await _create_e2b_session(client, monkeypatch)
    uploaded = await _upload(client, "input.txt", b"bounded input")
    counts_before = (provider.connect_count, provider.append_count, provider.pause_count)

    response = await _add(client, session["id"], uploaded["id"], mount_path)

    assert response.status_code == 422, response.text
    assert "/mnt/session/uploads/<filename>" in response.text
    assert (provider.connect_count, provider.append_count, provider.pause_count) == counts_before


async def test_idle_requires_action_window_allows_append(client, monkeypatch):
    session, provider = await _create_e2b_session(client, monkeypatch)
    await _set_session_state(
        session["id"],
        stop_reason={"type": "requires_action", "event_ids": ["event_tool"]},
    )
    uploaded = await _upload(client, "tool-result.txt", b"tool result attachment")

    response = await _add(
        client,
        session["id"],
        uploaded["id"],
        f"{UPLOAD_ROOT}/tool-result.txt",
    )

    assert response.status_code == 201, response.text
    assert provider.append_count == 1
    assert provider.sealed_revision == 1


@pytest.mark.parametrize(
    ("status", "archived"),
    [("running", False), ("rescheduling", False), ("idle", True)],
)
async def test_non_idle_or_archived_session_rejects_append_without_provider_touch(
    client,
    monkeypatch,
    status,
    archived,
):
    session, provider = await _create_e2b_session(client, monkeypatch)
    await _set_session_state(session["id"], status=status, archived=archived)
    uploaded = await _upload(client, "blocked.txt", b"must not mount")
    counts_before = (provider.connect_count, provider.append_count, provider.pause_count)

    response = await _add(
        client,
        session["id"],
        uploaded["id"],
        f"{UPLOAD_ROOT}/blocked.txt",
    )

    assert response.status_code == 409, response.text
    assert "only be added while the Session is idle and active" in response.text
    assert (provider.connect_count, provider.append_count, provider.pause_count) == counts_before


async def test_exact_retry_is_idempotent_and_revisions_are_monotonic(client, monkeypatch):
    session, provider = await _create_e2b_session(client, monkeypatch)
    first_file = await _upload(client, "first.txt", b"first immutable input")

    async with session_scope() as db:
        initial = await sandboxes_q.get_session_sandbox(
            db,
            session["id"],
            organization_id=ORGANIZATION_ID,
        )
        assert initial is not None
        external_id = initial.external_sandbox_id
        create_digest = initial.config["create_input_digest"]
        assert initial.config["immutable_manifest_revision"] == 0

    first = await _add(
        client,
        session["id"],
        first_file["id"],
        f"{UPLOAD_ROOT}/first.txt",
    )
    assert first.status_code == 201, first.text

    retry = await _add(
        client,
        session["id"],
        first_file["id"],
        f"{UPLOAD_ROOT}/first.txt",
    )
    assert retry.status_code == 201, retry.text
    assert retry.json()["id"] == first.json()["id"]
    assert provider.append_count == 1
    assert provider.sealed_revision == 1

    second_file = await _upload(client, "second.txt", b"second immutable input")
    second = await _add(
        client,
        session["id"],
        second_file["id"],
        f"{UPLOAD_ROOT}/second.txt",
    )
    assert second.status_code == 201, second.text

    async with session_scope() as db:
        current = await sandboxes_q.get_session_sandbox(
            db,
            session["id"],
            organization_id=ORGANIZATION_ID,
        )
        assert current is not None
        assert current.external_sandbox_id == external_id == provider.external_id
        assert current.config["create_input_digest"] == create_digest
        assert current.config["immutable_manifest_revision"] == 2
        assert set(current.config["immutable_manifest"]) == {
            f"{UPLOAD_ROOT}/first.txt",
            f"{UPLOAD_ROOT}/second.txt",
        }

    assert provider.provision_count == 1
    assert provider.bootstrap_count == 1
    assert provider.connect_count == 2
    assert provider.append_count == 2
    assert provider.sealed_revision == 2


async def test_persisted_descriptor_append_does_not_download_existing_file_or_skill(
    client,
    monkeypatch,
):
    skill_response = await client.post(
        "/v1/skills",
        headers=TEST_HEADERS,
        files={
            "files": (
                "skill/SKILL.md",
                b"---\nname: append-fast\ndescription: Existing skill.\n---\nUse it.",
                "text/markdown",
            )
        },
    )
    assert skill_response.status_code == 201, skill_response.text
    skill = skill_response.json()
    initial_file = await _upload(client, "initial.txt", b"existing session input")
    session, provider = await _create_e2b_session(
        client,
        monkeypatch,
        skills=[
            {
                "type": "custom",
                "skill_id": skill["id"],
                "version": skill["latest_version"],
            }
        ],
        resources=[
            {
                "type": "file",
                "file_id": initial_file["id"],
                "mount_path": f"{UPLOAD_ROOT}/initial.txt",
            }
        ],
    )

    async with session_scope() as db:
        mounted_resources = await res_q.list_resources(
            db,
            resource_type="session_resource",
            parent_id=session["id"],
            limit=1000,
            organization_id=ORGANIZATION_ID,
        )
        initial_resource = next(
            item for item in mounted_resources if (item.data or {}).get("type") == "file"
        )
        initial_key = str(
            (((initial_resource.data or {}).get("session_file") or {}).get("storage") or {}).get(
                "key"
            )
        )
        assert initial_key and initial_key != "None"
        skill_version = await res_q.get_resource_version(
            db,
            resource_type="skill_version",
            parent_id=skill["id"],
            version=int(skill["latest_version"]),
            organization_id=ORGANIZATION_ID,
        )
        assert skill_version is not None and skill_version.storage_key
        skill_key = skill_version.storage_key
        record = await sandboxes_q.get_session_sandbox(
            db,
            session["id"],
            organization_id=ORGANIZATION_ID,
        )
        assert record is not None
        descriptor = record.config["input_descriptor"]
        assert record.config["input_total_size_bytes"] == sum(
            item["size_bytes"] for item in descriptor["files"]
        )

    appended_file = await _upload(client, "appended.txt", b"only this input is new")
    original_download = storage.download_file
    downloaded_keys: list[str] = []

    async def tracked_download(key: str) -> bytes:
        downloaded_keys.append(key)
        return await original_download(key)

    monkeypatch.setattr(storage, "download_file", tracked_download)
    response = await _add(
        client,
        session["id"],
        appended_file["id"],
        f"{UPLOAD_ROOT}/appended.txt",
    )

    assert response.status_code == 201, response.text
    assert downloaded_keys
    assert initial_key not in downloaded_keys
    assert skill_key not in downloaded_keys
    assert provider.uploads[-1] == [
        (f"{UPLOAD_ROOT}/appended.txt", b"only this input is new")
    ]


async def test_legacy_binding_hydrates_once_then_backfills_descriptor(client, monkeypatch):
    initial_file = await _upload(client, "legacy-initial.txt", b"legacy input")
    session, provider = await _create_e2b_session(
        client,
        monkeypatch,
        resources=[
            {
                "type": "file",
                "file_id": initial_file["id"],
                "mount_path": f"{UPLOAD_ROOT}/legacy-initial.txt",
            }
        ],
    )
    async with session_scope() as db:
        record = await sandboxes_q.get_session_sandbox(
            db,
            session["id"],
            organization_id=ORGANIZATION_ID,
            for_update=True,
        )
        assert record is not None
        legacy_config = dict(record.config or {})
        legacy_config.pop("input_descriptor", None)
        legacy_config.pop("input_total_size_bytes", None)
        record.config = legacy_config
        resources = await res_q.list_resources(
            db,
            resource_type="session_resource",
            parent_id=session["id"],
            limit=1000,
            organization_id=ORGANIZATION_ID,
        )
        initial_resource = next(
            item for item in resources if (item.data or {}).get("type") == "file"
        )
        initial_key = str(
            (((initial_resource.data or {}).get("session_file") or {}).get("storage") or {}).get(
                "key"
            )
        )
        assert initial_key and initial_key != "None"
        await db.commit()

    original_download = storage.download_file
    downloaded_keys: list[str] = []

    async def tracked_download(key: str) -> bytes:
        downloaded_keys.append(key)
        return await original_download(key)

    monkeypatch.setattr(storage, "download_file", tracked_download)
    first_file = await _upload(client, "legacy-first.txt", b"first append")
    first = await _add(
        client,
        session["id"],
        first_file["id"],
        f"{UPLOAD_ROOT}/legacy-first.txt",
    )
    assert first.status_code == 201, first.text
    assert initial_key in downloaded_keys

    async with session_scope() as db:
        record = await sandboxes_q.get_session_sandbox(
            db,
            session["id"],
            organization_id=ORGANIZATION_ID,
        )
        assert record is not None
        assert record.config["input_descriptor"]["version"] == "vma-session-inputs-v1"
        assert record.config["input_total_size_bytes"] == (
            len(b"legacy input") + len(b"first append")
        )

    downloaded_keys.clear()
    second_file = await _upload(client, "legacy-second.txt", b"second append")
    second = await _add(
        client,
        session["id"],
        second_file["id"],
        f"{UPLOAD_ROOT}/legacy-second.txt",
    )
    assert second.status_code == 201, second.text
    assert initial_key not in downloaded_keys
    assert provider.sealed_revision == 2


async def test_exact_retry_recovers_provider_ahead_of_rolled_back_database(
    client,
    monkeypatch,
):
    from app.runtime.sandbox_providers import SandboxOperationError

    session, provider = await _create_e2b_session(client, monkeypatch)
    uploaded = await _upload(client, "retry.txt", b"provider ahead retry")
    original_update = sandboxes_q.update_session_sandbox_state
    failed_once = False

    async def fail_after_provider_once(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise SandboxOperationError("simulated database update failure")
        return await original_update(*args, **kwargs)

    monkeypatch.setattr(sandboxes_q, "update_session_sandbox_state", fail_after_provider_once)
    first = await _add(
        client,
        session["id"],
        uploaded["id"],
        f"{UPLOAD_ROOT}/retry.txt",
    )
    assert first.status_code == 503, first.text
    assert provider.sealed_revision == 1

    retried = await _add(
        client,
        session["id"],
        uploaded["id"],
        f"{UPLOAD_ROOT}/retry.txt",
    )
    assert retried.status_code == 201, retried.text
    assert provider.provision_count == 1
    assert provider.append_count == 2
    assert provider.sealed_revision == 1

    async with session_scope() as db:
        record = await sandboxes_q.get_session_sandbox(
            db,
            session["id"],
            organization_id=ORGANIZATION_ID,
        )
        assert record is not None
        assert record.external_sandbox_id == provider.external_id
        assert record.config["immutable_manifest_revision"] == 1


@pytest.mark.parametrize(
    "conflicting_path",
    [
        f"{UPLOAD_ROOT}/existing.txt",
        f"{UPLOAD_ROOT}/existing.txt/child.txt",
    ],
)
async def test_same_or_overlapping_mount_conflict_preserves_backend_error_phrase(
    client,
    monkeypatch,
    conflicting_path,
):
    session, provider = await _create_e2b_session(client, monkeypatch)
    original = await _upload(client, "original.txt", b"original")
    added = await _add(
        client,
        session["id"],
        original["id"],
        f"{UPLOAD_ROOT}/existing.txt",
    )
    assert added.status_code == 201, added.text
    replacement = await _upload(client, "replacement.txt", b"replacement")
    append_count = provider.append_count

    response = await _add(client, session["id"], replacement["id"], conflicting_path)

    assert response.status_code == 400, response.text
    assert "overlapping mount paths" in response.text
    assert provider.append_count == append_count


@pytest.mark.parametrize(
    ("copied_bytes", "expected_detail"),
    [
        (b"four", "size does not match"),
        (b"xyz", "sha256 does not match"),
    ],
)
async def test_copied_object_size_or_sha_drift_is_rejected_before_provider_touch(
    client,
    monkeypatch,
    copied_bytes,
    expected_detail,
):
    session, provider = await _create_e2b_session(client, monkeypatch)
    uploaded = await _upload(client, "source.txt", b"abc")

    async def drifted_download(_key: str) -> bytes:
        return copied_bytes

    monkeypatch.setattr(storage, "download_file", drifted_download)
    counts_before = (provider.connect_count, provider.append_count, provider.pause_count)

    response = await _add(
        client,
        session["id"],
        uploaded["id"],
        f"{UPLOAD_ROOT}/source.txt",
    )

    assert response.status_code == 422, response.text
    assert expected_detail in response.text
    assert (provider.connect_count, provider.append_count, provider.pause_count) == counts_before


async def test_aggregate_session_input_limit_bounds_append_memory(client, monkeypatch):
    session, provider = await _create_e2b_session(client, monkeypatch)
    monkeypatch.setenv("VMA_MAX_SESSION_INPUT_BYTES", "5")
    get_settings.cache_clear()
    first = await _upload(client, "first.txt", b"abc")
    second = await _upload(client, "second.txt", b"def")

    response = await _add(
        client,
        session["id"],
        first["id"],
        f"{UPLOAD_ROOT}/first.txt",
    )
    assert response.status_code == 201, response.text
    append_count = provider.append_count

    response = await _add(
        client,
        session["id"],
        second["id"],
        f"{UPLOAD_ROOT}/second.txt",
    )
    assert response.status_code == 413, response.text
    assert "aggregate size of 5 bytes" in response.text
    assert provider.append_count == append_count


@pytest.mark.parametrize(
    ("sandbox_state", "provider_name", "expected_status", "expected_detail"),
    [
        ("running", "e2b", 409, "must be paused"),
        ("paused", "state", 422, "does not support append-only"),
    ],
)
async def test_non_paused_or_wrong_provider_binding_rejects_append_without_provider_touch(
    client,
    monkeypatch,
    sandbox_state,
    provider_name,
    expected_status,
    expected_detail,
):
    session, provider = await _create_e2b_session(client, monkeypatch)
    async with session_scope() as db:
        record = await sandboxes_q.get_session_sandbox(
            db,
            session["id"],
            organization_id=ORGANIZATION_ID,
            for_update=True,
        )
        assert record is not None
        record.state = sandbox_state
        record.provider = provider_name
        await db.commit()

    uploaded = await _upload(client, "binding.txt", b"binding check")
    counts_before = (provider.connect_count, provider.append_count, provider.pause_count)

    response = await _add(
        client,
        session["id"],
        uploaded["id"],
        f"{UPLOAD_ROOT}/binding.txt",
    )

    assert response.status_code == expected_status, response.text
    assert expected_detail in response.text
    assert (provider.connect_count, provider.append_count, provider.pause_count) == counts_before
