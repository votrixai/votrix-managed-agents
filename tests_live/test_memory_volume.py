"""Real E2B proof of the complete Memory API/Volume/Sandbox round trip.

Volumes are still an E2B private-beta feature, so this scenario is opt-in even
inside the live suite. Run it with ``VMA_TEST_E2B_VOLUMES=1`` after E2B has
enabled Volumes for the project behind ``E2B_API_KEY``.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.models import SessionMemoryStore, SessionSandbox
from app.db.queries import memory as memory_q
from app.services import memory_records
from app.utils.sandbox import Sandbox
from app.utils.volume import Volume

pytestmark = pytest.mark.asyncio(loop_scope="session")

if os.environ.get("VMA_TEST_E2B_VOLUMES") != "1":
    pytest.skip(
        "set VMA_TEST_E2B_VOLUMES=1 for the E2B private-beta Volume scenario",
        allow_module_level=True,
    )


@pytest.fixture(scope="session")
def settings():
    """This scenario needs E2B and a database, but never calls a model or S3."""
    from app.config import get_settings

    resolved = get_settings()
    missing = [
        name
        for name in ("database_url", "e2b_api_key")
        if not str(getattr(resolved, name, "")).strip()
    ]
    if missing:
        pytest.skip("live test needs: " + ", ".join(name.upper() for name in missing))
    if not str(resolved.database_url).startswith(("postgresql", "postgres://")):
        pytest.skip(
            f"live test needs Postgres, DATABASE_URL is {resolved.database_url!r}"
        )
    return resolved


@pytest_asyncio.fixture(loop_scope="session")
async def memory_store(api, organization):
    response = await api.post(
        "/v1/memory_stores",
        json={
            "name": f"Live Memory {uuid.uuid4().hex[:8]}",
            "description": "An opt-in E2B Volume persistence check.",
        },
    )
    response.raise_for_status()
    created = response.json()
    yield created

    # This suite retains Session attachment snapshots, so the public delete
    # intentionally refuses the Store. Tear down every actual Sandbox first,
    # then clean the private-beta provider resource and leave a DB tombstone.
    from app.db.engine import session_scope

    async with session_scope() as db:
        store = await memory_q.get_memory_store(
            db,
            memory_store_id=created["id"],
            organization_id=organization,
        )
        sandboxes = list(
            (
                await db.execute(
                    select(SessionSandbox)
                    .join(
                        SessionMemoryStore,
                        SessionMemoryStore.session_id == SessionSandbox.session_id,
                    )
                    .where(SessionMemoryStore.memory_store_id == created["id"])
                )
            )
            .scalars()
            .all()
        )
        for row in sandboxes:
            if row.external_sandbox_id:
                try:
                    await Sandbox.from_id(
                        row.external_sandbox_id,
                        row.session_id,
                        row.organization_id,
                    ).kill()
                except Exception:
                    pass
        await Volume.destroy(store)
        await memory_q.purge_memory_store_contents(
            db,
            memory_store_id=store.id,
            organization_id=store.organization_id,
        )
        await memory_q.mark_memory_store_deleted(db, store)
        await db.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def new_memory_session(api):
    """Create only the control-plane records needed to provision a Sandbox."""
    environment_response = await api.post(
        "/v1/environments",
        json={"name": f"memory-live-{uuid.uuid4().hex[:8]}", "config": {}},
    )
    environment_response.raise_for_status()
    environment = environment_response.json()
    assert environment["build_state"] == "ready", environment

    agent_response = await api.post(
        "/v1/agents",
        json={
            "name": f"memory-live-{uuid.uuid4().hex[:8]}",
            "model": "claude-opus-5",
            "system": "Memory Volume live smoke test.",
            "tools": [],
            "skills": [],
        },
    )
    agent_response.raise_for_status()
    agent = agent_response.json()
    made: list[str] = []

    async def _create(*, resources):
        response = await api.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "environment_id": environment["id"],
                "resources": resources,
            },
        )
        response.raise_for_status()
        session_id = response.json()["id"]
        made.append(session_id)
        return session_id

    yield _create

    for session_id in made:
        try:
            await (await _container(session_id)).kill()
        except Exception:
            pass
        await api.delete(f"/v1/sessions/{session_id}")
    await api.post(f"/v1/agents/{agent['id']}/archive")
    await api.post(f"/v1/environments/{environment['id']}/archive")


async def _container(session_id: str) -> Sandbox:
    from app.db.engine import session_scope

    async with session_scope() as db:
        row = (
            await db.execute(
                select(SessionSandbox).where(SessionSandbox.session_id == session_id)
            )
        ).scalar_one()
        return Sandbox.from_id(
            row.external_sandbox_id,
            row.session_id,
            row.organization_id,
        )


async def test_memory_api_seed_mount_runtime_update_and_version_round_trip(
    api, organization, new_memory_session, memory_store
):
    resource = {
        "type": "memory_store",
        "memory_store_id": memory_store["id"],
        "access": "read_write",
        "instructions": "Use context.md for durable project context.",
    }
    seed = f"VMA-MEMORY-SEED-{uuid.uuid4().hex}"
    updated = f"VMA-MEMORY-UPDATED-{uuid.uuid4().hex}"
    mount_path = f"{memory_store['name'].lower().replace(' ', '-')}"
    path = f"/mnt/memory/{mount_path}/context.md"

    created_response = await api.post(
        f"/v1/memory_stores/{memory_store['id']}/memories",
        json={"path": "/context.md", "content": seed},
    )
    created_response.raise_for_status()
    created = created_response.json()

    first_id = await new_memory_session(resources=[resource])
    first = await _container(first_id)
    assert await first.read_bytes(path, max_bytes=1024) == seed.encode()

    edited = await first.to_deep_agent_backend.aedit(path, seed, updated)
    assert edited.error is None, edited

    from app.db.engine import session_scope

    async with session_scope() as db:
        synced = await memory_records.reconcile_session_memory_stores(
            db,
            session_id=first_id,
            organization_id=organization,
            sandbox=first,
        )
    assert synced.changed == 1

    indexed = await api.get(
        f"/v1/memory_stores/{memory_store['id']}/memories/{created['id']}",
    )
    indexed.raise_for_status()
    assert indexed.json()["path"] == "/context.md"
    assert indexed.json()["content"] == updated

    versions_response = await api.get(
        f"/v1/memory_stores/{memory_store['id']}/memory_versions",
        params={"memory_id": created["id"], "view": "full"},
    )
    versions_response.raise_for_status()
    versions = versions_response.json()["data"]
    assert [item["operation"] for item in versions] == ["modified", "created"]
    assert versions[0]["content"] == updated
    assert versions[0]["created_by"] == {
        "type": "session_actor",
        "session_id": first_id,
    }
    assert versions[1]["content"] == seed

    await first.kill()

    second_id = await new_memory_session(resources=[resource])
    second = await _container(second_id)
    assert await second.read_bytes(path, max_bytes=1024) == updated.encode()
