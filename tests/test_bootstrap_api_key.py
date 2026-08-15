from contextlib import asynccontextmanager

import pytest

from app.db.queries import vma_api_keys as keys
import scripts.bootstrap_api_key as bootstrap


@pytest.fixture
def bootstrap_with_test_db(monkeypatch, db):
    @asynccontextmanager
    async def session_scope():
        try:
            yield db
        except Exception:
            await db.rollback()
            raise

    monkeypatch.setattr(bootstrap, "session_scope", session_scope)


async def test_bootstrap_creates_and_idempotently_reuses_management_key(
    db,
    bootstrap_with_test_db,
):
    created = await bootstrap.bootstrap_api_key(
        organization_id="org_bootstrap",
        organization_name="Bootstrap",
    )

    assert created.organization_created
    keys.validate_vma_api_key(created.secret)
    rows = await keys.list_vma_api_keys(db, organization_id="org_bootstrap")
    assert len(rows) == 1
    assert rows[0].id == created.key_id
    assert rows[0].key_hash == keys.hash_vma_api_key(created.secret)
    assert created.secret not in {str(value) for value in rows[0].__dict__.values()}

    repeated = await bootstrap.bootstrap_api_key(
        organization_id="org_bootstrap",
        api_key=created.secret,
    )
    assert repeated.key_id == created.key_id
    assert not repeated.organization_created
    assert len(
        await keys.list_vma_api_keys(db, organization_id="org_bootstrap")
    ) == 1

    with pytest.raises(bootstrap.BootstrapConflict, match="already has"):
        await bootstrap.bootstrap_api_key(
            organization_id="org_bootstrap",
        )


async def test_bootstrap_accepts_preprovisioned_key(
    db,
    bootstrap_with_test_db,
):
    secret = "vma_" + "x" * keys.VMA_API_KEY_RANDOM_LENGTH

    imported = await bootstrap.bootstrap_api_key(
        organization_id="org_unified_bootstrap",
        organization_name="Unified bootstrap",
        api_key=secret,
    )

    assert imported.secret == secret
    assert await keys.get_vma_api_key_by_token(db, secret) is not None
