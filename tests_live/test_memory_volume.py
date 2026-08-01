"""Real E2B proof that one Memory Store survives a Sandbox replacement.

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


@pytest_asyncio.fixture(loop_scope="session")
async def memory_store(api):
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
            organization_id=api.headers["x-organization-id"],
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


async def test_a_write_is_visible_after_the_first_sandbox_is_destroyed(
    api, new_session, memory_store
):
    resource = {
        "type": "memory_store",
        "memory_store_id": memory_store["id"],
        "access": "read_write",
        "instructions": "Use context.md for durable project context.",
    }
    token = f"VMA-MEMORY-{uuid.uuid4().hex}"
    mount_path = f"{memory_store['name'].lower().replace(' ', '-')}"
    path = f"/mnt/memory/{mount_path}/context.md"

    first_id = await new_session(resources=[resource])
    first = await _container(first_id)
    written = await first.to_deep_agent_backend.awrite(path, token)
    assert written.error is None, written

    from app.db.engine import session_scope

    async with session_scope() as db:
        synced = await memory_records.reconcile_session_memory_stores(
            db,
            session_id=first_id,
            organization_id=api.headers["x-organization-id"],
            sandbox=first,
        )
    assert synced.changed == 1

    indexed = await api.get(
        f"/v1/memory_stores/{memory_store['id']}/memories",
        params={"view": "full"},
    )
    indexed.raise_for_status()
    assert [(item["path"], item["content"]) for item in indexed.json()["data"]] == [
        ("/context.md", token)
    ]

    await first.kill()

    second_id = await new_session(resources=[resource])
    second = await _container(second_id)
    assert await second.read_bytes(path, max_bytes=1024) == token.encode()
