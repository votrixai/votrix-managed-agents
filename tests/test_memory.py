"""Memory Store metadata, tenant boundaries, and Volume lifecycle persistence."""

from __future__ import annotations

from app.db.models.memory import (
    VOLUME_DELETED,
    VOLUME_FAILED,
    VOLUME_PROVISIONING,
    VOLUME_READY,
)
from app.db.queries import accounts
from app.db.queries import memory as memory_q


async def test_a_store_starts_before_its_provider_volume_exists(db, org):
    store = await memory_q.create_memory_store(
        db,
        organization_id=org,
        name="User preferences",
        description="Long-lived preferences",
        metadata={"owner": "user_1"},
        volume_provider="e2b",
    )
    await db.commit()

    assert store.id.startswith("memstore_")
    assert store.volume_provider == "e2b"
    assert store.volume_locator is None
    assert store.provisioning_status == VOLUME_PROVISIONING
    assert store.metadata_ == {"owner": "user_1"}


async def test_provider_completion_records_a_structured_volume_locator(db, org):
    store = await memory_q.create_memory_store(
        db, organization_id=org, name="Project memory"
    )

    await memory_q.set_volume_state(
        db,
        store,
        status=VOLUME_READY,
        volume_locator={"volume_id": "vol_123", "volume_name": "vma-memstore-123"},
        error=None,
    )
    await db.commit()

    assert store.volume_locator == {
        "volume_id": "vol_123",
        "volume_name": "vma-memstore-123",
    }
    assert store.provisioning_error is None
    assert store.lock_version == 1

    await memory_q.set_volume_state(
        db,
        store,
        status=VOLUME_READY,
        volume_locator={"volume_id": "vol_123", "volume_name": "vma-memstore-123"},
        error=None,
    )
    assert store.lock_version == 1


async def test_provider_failure_is_persisted_for_a_retry(db, org):
    store = await memory_q.create_memory_store(
        db, organization_id=org, name="Project memory"
    )

    await memory_q.set_volume_state(
        db,
        store,
        status=VOLUME_FAILED,
        error="provider unavailable",
    )
    await db.commit()

    assert store.provisioning_status == VOLUME_FAILED
    assert store.provisioning_error == "provider unavailable"
    assert store.volume_locator is None


async def test_a_store_can_only_be_read_inside_its_organization(db, org):
    other = await accounts.create_organization(db, slug="other", name="Other")
    store = await memory_q.create_memory_store(
        db, organization_id=org, name="Private memory"
    )
    await db.commit()

    assert (
        await memory_q.get_memory_store(
            db, memory_store_id=store.id, organization_id=other.id
        )
        is None
    )
    assert (
        await memory_q.get_memory_store(
            db, memory_store_id=store.id, organization_id=org
        )
        is store
    )


async def test_normal_lists_hide_archived_and_deleted_stores(db, org):
    active = await memory_q.create_memory_store(db, organization_id=org, name="Active")
    archived = await memory_q.create_memory_store(
        db, organization_id=org, name="Archived"
    )
    deleted = await memory_q.create_memory_store(db, organization_id=org, name="Deleted")
    await memory_q.archive_memory_store(db, archived)
    await memory_q.mark_memory_store_deleted(db, deleted)
    await db.commit()

    normal = await memory_q.list_memory_stores(db, organization_id=org)
    complete = await memory_q.list_memory_stores(
        db,
        organization_id=org,
        include_archived=True,
        include_deleted=True,
    )

    assert [store.id for store in normal.items] == [active.id]
    assert {store.id for store in complete.items} == {active.id, archived.id, deleted.id}
    assert deleted.provisioning_status == VOLUME_DELETED


async def test_an_unknown_provider_state_is_rejected_before_flush(db, org):
    store = await memory_q.create_memory_store(db, organization_id=org, name="Memory")

    try:
        await memory_q.set_volume_state(db, store, status="mystery")
    except ValueError as exc:
        assert "mystery" in str(exc)
    else:
        raise AssertionError("unknown states must not reach the database")


async def test_an_unknown_volume_provider_is_rejected_before_flush(db, org):
    try:
        await memory_q.create_memory_store(
            db,
            organization_id=org,
            name="Memory",
            volume_provider="unknown",
        )
    except ValueError as exc:
        assert "unknown" in str(exc)
    else:
        raise AssertionError("unknown providers must not reach the database")
