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
        organization_slug="bootstrap",
        organization_name="Bootstrap",
    )

    assert created.organization_created
    assert created.secret.startswith("vma_")
    assert not created.secret.startswith(keys.LEGACY_VMA_API_KEY_PREFIXES)
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


async def test_legacy_bootstrap_requires_explicit_empty_org_import(
    db,
    org,
    bootstrap_with_test_db,
):
    legacy_secret = "vma_live_" + "x" * 32

    with pytest.raises(ValueError, match="allow-legacy-import"):
        await bootstrap.bootstrap_api_key(
            organization_id=org,
            api_key=legacy_secret,
        )

    imported = await bootstrap.bootstrap_api_key(
        organization_id=org,
        api_key=legacy_secret,
        allow_legacy_import=True,
    )
    assert imported.secret == legacy_secret
    stored = await keys.get_vma_api_key_by_token(db, legacy_secret)
    assert stored is not None
    assert stored.key_hash == keys.hash_vma_api_key(legacy_secret)
    assert stored.prefix != legacy_secret

    await keys.revoke_vma_api_key(db, stored, reason="test")
    await db.commit()
    with pytest.raises(bootstrap.BootstrapConflict, match="before the Organization"):
        await bootstrap.bootstrap_api_key(
            organization_id=org,
            api_key="vma_test_" + "y" * 32,
            allow_legacy_import=True,
        )


async def test_bootstrap_accepts_unified_preprovisioned_key_without_legacy_flag(
    db,
    bootstrap_with_test_db,
):
    secret = "vma_" + "x" * 32

    imported = await bootstrap.bootstrap_api_key(
        organization_id="org_unified_bootstrap",
        organization_slug="unified-bootstrap",
        organization_name="Unified bootstrap",
        api_key=secret,
    )

    assert imported.secret == secret
    assert await keys.get_vma_api_key_by_token(db, secret) is not None
