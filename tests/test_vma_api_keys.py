from datetime import datetime, timedelta, timezone

import pytest

from app.services import organizations as organizations_service
from tests.conftest import FakeKeys
from sqlalchemy.exc import IntegrityError

from app.config import clear_settings_cache
from app.db.queries import organizations
from app.db.queries import vma_api_keys as keys


def test_vma_api_key_generation_uses_environment_prefix(monkeypatch):
    assert keys.generate_vma_api_key(app_env="staging").startswith("vma_test_")
    assert keys.generate_vma_api_key(app_env="production").startswith("vma_live_")

    monkeypatch.setenv("APP_ENV", "production")
    clear_settings_cache()
    try:
        assert keys.generate_vma_api_key().startswith("vma_live_")
    finally:
        clear_settings_cache()


def test_vma_api_key_scope_and_prefix_validation():
    assert keys.normalize_vma_api_key_scopes(
        [keys.VMA_WORKER_SCOPE, keys.VMA_API_SCOPE, keys.VMA_API_SCOPE]
    ) == (keys.VMA_API_SCOPE, keys.VMA_WORKER_SCOPE)

    with pytest.raises(ValueError, match="At least one"):
        keys.normalize_vma_api_key_scopes([])
    with pytest.raises(ValueError, match="Unknown"):
        keys.normalize_vma_api_key_scopes(["root"])
    with pytest.raises(ValueError, match="collection"):
        keys.normalize_vma_api_key_scopes("api")
    with pytest.raises(ValueError, match="vma_test_"):
        keys.validate_vma_api_key_prefix(
            "vma_live_" + "x" * 32,
            app_env="staging",
        )
    with pytest.raises(ValueError, match="at least 32"):
        keys.validate_legacy_vma_api_key("vma_too_short")


async def test_create_vma_api_key_only_persists_hash(db, org):
    api_key, plaintext = await keys.create_vma_api_key(
        db,
        organization_id=org,
        name="Backend",
        scopes=[keys.VMA_API_KEYS_MANAGE_SCOPE, keys.VMA_API_SCOPE],
        created_by="user_1",
        metadata={"purpose": "integration"},
    )
    await db.commit()

    assert plaintext.startswith("vma_test_")
    assert api_key.key_hash == keys.hash_vma_api_key(plaintext)
    assert api_key.prefix == plaintext[: keys.DISPLAYED_VMA_API_KEY_PREFIX_LENGTH]
    assert api_key.scopes == [keys.VMA_API_SCOPE, keys.VMA_API_KEYS_MANAGE_SCOPE]
    assert api_key.metadata_ == {"purpose": "integration"}
    assert plaintext not in {str(value) for value in api_key.__dict__.values()}
    assert not hasattr(api_key, "secret")

    resolved = await keys.get_vma_api_key_by_token(db, plaintext)
    assert resolved is api_key


async def test_vma_api_key_queries_are_tenant_scoped(db, org):
    other = await organizations_service.create_organization(db, keys=FakeKeys(), slug="other-keys", name="Other")
    first, _ = await keys.create_vma_api_key(
        db,
        organization_id=org,
        name="First",
    )
    second, _ = await keys.create_vma_api_key(
        db,
        organization_id=other.id,
        name="Second",
    )
    await db.commit()

    assert await keys.list_vma_api_keys(db, organization_id=org) == [first]
    assert await keys.list_vma_api_keys(db, organization_id=other.id) == [second]
    assert (
        await keys.get_vma_api_key(
            db,
            organization_id=other.id,
            key_id=first.id,
        )
        is None
    )


async def test_revoke_is_idempotent_and_removes_key_from_active_lookup(db, org):
    api_key, plaintext = await keys.create_vma_api_key(
        db,
        organization_id=org,
        name="Revocable",
    )
    await db.commit()

    await keys.revoke_vma_api_key(
        db,
        api_key,
        revoked_by="user_1",
        reason="retired",
    )
    first_revoked_at = api_key.revoked_at
    await keys.revoke_vma_api_key(
        db,
        api_key,
        revoked_by="user_2",
        reason="must not overwrite",
    )

    assert api_key.revoked_at == first_revoked_at
    assert api_key.revoked_by == "user_1"
    assert api_key.revocation_reason == "retired"
    assert await keys.get_vma_api_key_by_token(db, plaintext) is None
    assert await keys.get_vma_api_key_by_token(
        db,
        plaintext,
        include_inactive=True,
    ) is api_key
    assert await keys.list_vma_api_keys(
        db,
        organization_id=org,
        include_revoked=False,
    ) == []


async def test_rotate_vma_api_key_allows_no_downtime_cutover(db, org):
    original, original_plaintext = await keys.create_vma_api_key(
        db,
        organization_id=org,
        name="Rotating",
        scopes=[keys.VMA_API_SCOPE, keys.VMA_WORKER_SCOPE],
        created_by="user_1",
        metadata={"system": "backend"},
    )
    await db.commit()

    replacement, replacement_plaintext = await keys.rotate_vma_api_key(
        db,
        organization_id=org,
        key_id=original.id,
        created_by="user_2",
    )
    await db.commit()

    assert replacement_plaintext != original_plaintext
    assert replacement.replaces_key_id == original.id
    assert replacement.scopes == original.scopes
    assert replacement.metadata_ == original.metadata_
    assert replacement.metadata_ is not original.metadata_
    assert replacement.expires_at is None
    assert original.revoked_at is None
    assert await keys.get_vma_api_key_by_token(db, original_plaintext) is original
    assert await keys.get_vma_api_key_by_token(db, replacement_plaintext) is replacement

    await keys.revoke_vma_api_key(
        db,
        original,
        revoked_by="user_2",
        reason="rotation cutover complete",
    )
    assert original.revoked_at is not None
    assert original.revoked_by == "user_2"
    assert original.revocation_reason == "rotation cutover complete"

    with pytest.raises(ValueError, match="revoked"):
        await keys.rotate_vma_api_key(
            db,
            organization_id=org,
            key_id=original.id,
        )


async def test_only_one_key_can_replace_a_generation(db, org):
    original, _ = await keys.create_vma_api_key(
        db,
        organization_id=org,
        name="Original",
    )
    await db.commit()

    await keys.create_vma_api_key(
        db,
        organization_id=org,
        name="First replacement",
        replaces_key_id=original.id,
    )
    await db.commit()

    with pytest.raises(IntegrityError):
        await keys.create_vma_api_key(
            db,
            organization_id=org,
            name="Second replacement",
            replaces_key_id=original.id,
        )
    await db.rollback()


async def test_replacement_lineage_cannot_cross_organizations(db, org):
    other = await organizations_service.create_organization(
        db,
        keys=FakeKeys(),
        slug="other-lineage",
        name="Other lineage",
    )
    original, _ = await keys.create_vma_api_key(
        db,
        organization_id=org,
        name="Original",
    )
    await db.commit()

    with pytest.raises(ValueError, match="same Organization"):
        await keys.create_vma_api_key(
            db,
            organization_id=other.id,
            name="Invalid replacement",
            replaces_key_id=original.id,
        )


async def test_vma_api_key_expiry_is_enforced(db, org):
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="future"):
        await keys.create_vma_api_key(
            db,
            organization_id=org,
            name="Already expired",
            expires_at=now - timedelta(seconds=1),
        )

    api_key, plaintext = await keys.create_vma_api_key(
        db,
        organization_id=org,
        name="Expiring",
        expires_at=now + timedelta(hours=1),
    )
    assert not keys.vma_api_key_is_expired(api_key, now=now)

    api_key.expires_at = now - timedelta(seconds=1)
    await db.commit()
    assert keys.vma_api_key_is_expired(api_key, now=now)
    assert await keys.get_vma_api_key_by_token(db, plaintext) is None
