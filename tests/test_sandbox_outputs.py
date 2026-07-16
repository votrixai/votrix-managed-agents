import hashlib

import pytest

from app import storage
from app.db.engine import session_scope
from app.db.queries import agents as agents_q
from app.db.queries import environments as environments_q
from app.db.queries import resources as res_q
from app.db.queries import sessions as sessions_q
from app.runtime import sandbox_outputs
from app.runtime.sandbox_outputs import (
    DiscoveredSandboxOutput,
    SandboxOutputValidationError,
    persist_discovered_outputs,
)
from tests.conftest import TEST_HEADERS


async def _create_session(db, *, organization_id: str = "org_test"):
    agent, version = await agents_q.create_agent(
        db,
        name="Sandbox output agent",
        model={"id": "deepseek/deepseek-v4-pro"},
        organization_id=organization_id,
    )
    environment = await environments_q.create_environment(
        db,
        name="Sandbox output environment",
        config={"type": "cloud"},
        organization_id=organization_id,
    )
    return await sessions_q.create_session(
        db,
        agent=agent,
        agent_version=version.version,
        environment=environment,
        organization_id=organization_id,
    )


async def test_persist_output_creates_downloadable_session_file(client):
    content = b"customer,value\nacme,42\n"
    digest = hashlib.sha256(content).hexdigest()

    async with session_scope() as db:
        session = await _create_session(db)
        created = await persist_discovered_outputs(
            db,
            session,
            [
                DiscoveredSandboxOutput(
                    path="/mnt/session/outputs/report.csv",
                    content=content,
                    mime_type="text/csv",
                )
            ],
        )
        await db.commit()
        session_id = session.id
        output = created[0]
        output_id = output.id

        assert output.parent_id == session_id
        assert output.organization_id == session.organization_id
        assert output.filename == "report.csv"
        assert output.content_type == "text/csv"
        assert output.sha256 == digest
        assert output.size_bytes == len(content)
        assert f"/sessions_{session_id}/outputs/" in output.storage_key
        assert f"/{digest[:16]}_report.csv" in output.storage_key
        assert output.data == {
            "filename": "report.csv",
            "mime_type": "text/csv",
            "downloadable": True,
            "sandbox_path": "/mnt/session/outputs/report.csv",
            "scope": {"type": "session", "id": session_id},
            "source": "sandbox_output",
            "sha256": digest,
        }

    listed = await client.get(
        "/v1/files",
        params={"scope_id": session_id},
        headers=TEST_HEADERS,
    )
    assert listed.status_code == 200, listed.text
    item = next(item for item in listed.json()["data"] if item["id"] == output_id)
    assert item["downloadable"] is True
    assert item["scope"] == {"type": "session", "id": session_id}
    assert item["sandbox_path"] == "/mnt/session/outputs/report.csv"

    downloaded = await client.get(f"/v1/files/{output_id}/content", headers=TEST_HEADERS)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == content


async def test_scoped_list_does_not_prelimit_on_unrelated_organization_files(client):
    async with session_scope() as db:
        session = await _create_session(db)
        output = (
            await persist_discovered_outputs(
                db,
                session,
                [
                    DiscoveredSandboxOutput(
                        path="/mnt/session/outputs/important.txt",
                        content=b"must remain discoverable",
                    )
                ],
            )
        )[0]
        for index in range(1001):
            await res_q.create_resource(
                db,
                resource_type="file",
                name=f"unrelated-{index}.txt",
                filename=f"unrelated-{index}.txt",
                organization_id=session.organization_id,
            )
        await db.commit()
        session_id = session.id
        output_id = output.id

    response = await client.get(
        "/v1/files",
        params={"scope_id": session_id},
        headers=TEST_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["data"]] == [output_id]


async def test_exact_path_and_hash_retry_is_idempotent(monkeypatch):
    calls: list[dict] = []
    save_file_bytes = storage.save_file_bytes

    async def recording_save(data, mime_type, **kwargs):
        calls.append({"data": data, "mime_type": mime_type, **kwargs})
        return await save_file_bytes(data, mime_type, **kwargs)

    monkeypatch.setattr(storage, "save_file_bytes", recording_save)
    entry = DiscoveredSandboxOutput(
        path="/mnt/session/outputs/summary.txt",
        content=b"same snapshot",
        mime_type="text/plain",
    )

    async with session_scope() as db:
        session = await _create_session(db)
        first = await persist_discovered_outputs(db, session, [entry, entry])
        await db.commit()
        second = await persist_discovered_outputs(db, session, [entry])
        await db.commit()
        rows = await res_q.list_resources(
            db,
            resource_type="file",
            parent_id=session.id,
            organization_id=session.organization_id,
            limit=20,
        )

    assert len(first) == 1
    assert second == []
    assert len(rows) == 1
    assert len(calls) == 1


async def test_overwrite_keeps_old_file_and_scoped_list_puts_latest_first(client):
    path = "/mnt/session/outputs/report.txt"

    async with session_scope() as db:
        session = await _create_session(db)
        first = (
            await persist_discovered_outputs(
                db,
                session,
                [DiscoveredSandboxOutput(path=path, content=b"version one", mime_type="text/plain")],
            )
        )[0]
        await db.commit()
        second = (
            await persist_discovered_outputs(
                db,
                session,
                [DiscoveredSandboxOutput(path=path, content=b"version two", mime_type="text/plain")],
            )
        )[0]
        await db.commit()
        session_id = session.id
        first_id = first.id
        second_id = second.id

        assert first_id != second_id
        assert first.storage_key != second.storage_key
        assert first.created_at < second.created_at
        assert await res_q.get_resource(
            db,
            resource_id=first_id,
            resource_type="file",
            organization_id=session.organization_id,
        ) is not None
        assert await res_q.get_resource(
            db,
            resource_id=second_id,
            resource_type="file",
            organization_id=session.organization_id,
        ) is not None

    listed = await client.get(
        "/v1/files",
        params={"scope_id": session_id, "limit": 20},
        headers=TEST_HEADERS,
    )
    assert listed.status_code == 200, listed.text
    versions = [
        item
        for item in listed.json()["data"]
        if item.get("sandbox_path") == path
    ]
    assert [item["id"] for item in versions] == [second_id, first_id]
    assert versions[0]["created_at"] > versions[1]["created_at"]

    old_download = await client.get(f"/v1/files/{first_id}/content", headers=TEST_HEADERS)
    latest_download = await client.get(f"/v1/files/{second_id}/content", headers=TEST_HEADERS)
    assert old_download.content == b"version one"
    assert latest_download.content == b"version two"


@pytest.mark.parametrize(
    "entry",
    [
        DiscoveredSandboxOutput(path="relative.txt", content=b"x"),
        DiscoveredSandboxOutput(path="/mnt/session/outputs", content=b"x"),
        DiscoveredSandboxOutput(path="/mnt/session/outputs/nested/file.txt", content=b"x"),
        DiscoveredSandboxOutput(path="/mnt/session/outputs/../secret.txt", content=b"x"),
        DiscoveredSandboxOutput(path="/mnt/session/outputs//file.txt", content=b"x"),
        DiscoveredSandboxOutput(
            path="/mnt/session/outputs/link.txt",
            content=b"x",
            is_symlink=True,
        ),
        DiscoveredSandboxOutput(
            path="/mnt/session/outputs/directory",
            content=b"x",
            is_regular_file=False,
        ),
        DiscoveredSandboxOutput(
            path="/mnt/session/outputs/hardlink.txt",
            content=b"x",
            hardlink_count=2,
        ),
    ],
)
async def test_rejects_non_direct_or_untrusted_output_entries(entry):
    async with session_scope() as db:
        session = await _create_session(db)
        with pytest.raises(SandboxOutputValidationError):
            await persist_discovered_outputs(db, session, [entry])


async def test_validates_entire_batch_before_writing_storage(monkeypatch):
    save = monkeypatch.setattr(storage, "save_file_bytes", pytest.fail)

    async with session_scope() as db:
        session = await _create_session(db)
        with pytest.raises(SandboxOutputValidationError):
            await persist_discovered_outputs(
                db,
                session,
                [
                    DiscoveredSandboxOutput(
                        path="/mnt/session/outputs/valid.txt",
                        content=b"would otherwise be valid",
                    ),
                    DiscoveredSandboxOutput(
                        path="/mnt/session/outputs/nested/invalid.txt",
                        content=b"invalid",
                    ),
                ],
            )

    assert save is None


async def test_enforces_count_per_file_and_total_size_bounds(monkeypatch):
    async with session_scope() as db:
        session = await _create_session(db)

        monkeypatch.setattr(sandbox_outputs, "MAX_DISCOVERED_OUTPUT_FILES", 1)
        with pytest.raises(SandboxOutputValidationError, match="At most 1"):
            await persist_discovered_outputs(
                db,
                session,
                [
                    DiscoveredSandboxOutput(path="/mnt/session/outputs/a.txt", content=b"a"),
                    DiscoveredSandboxOutput(path="/mnt/session/outputs/b.txt", content=b"b"),
                ],
            )

        monkeypatch.setattr(sandbox_outputs, "MAX_DISCOVERED_OUTPUT_FILES", 100)
        monkeypatch.setattr(sandbox_outputs, "MAX_OUTPUT_FILE_BYTES", 3)
        with pytest.raises(SandboxOutputValidationError, match="file exceeds 3 bytes"):
            await persist_discovered_outputs(
                db,
                session,
                [DiscoveredSandboxOutput(path="/mnt/session/outputs/a.txt", content=b"four")],
            )

        monkeypatch.setattr(sandbox_outputs, "MAX_OUTPUT_FILE_BYTES", 100)
        monkeypatch.setattr(sandbox_outputs, "MAX_OUTPUT_TOTAL_BYTES", 5)
        with pytest.raises(SandboxOutputValidationError, match="batch exceeds 5 bytes"):
            await persist_discovered_outputs(
                db,
                session,
                [
                    DiscoveredSandboxOutput(path="/mnt/session/outputs/a.txt", content=b"abc"),
                    DiscoveredSandboxOutput(path="/mnt/session/outputs/b.txt", content=b"def"),
                ],
            )
