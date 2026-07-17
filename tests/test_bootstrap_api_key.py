import pytest
from sqlalchemy import func, select

from app.db.engine import session_scope
from app.db.models import ApiKey, Organization
from app.db.queries import api_keys as api_keys_q
from scripts.bootstrap_api_key import BootstrapConflict, bootstrap_api_key


@pytest.mark.parametrize(
    "organization_id",
    [
        "tenant",
        "org_",
        "org_bad/path",
        f"org_{'x' * 61}",
        "org_" + "default",
    ],
)
async def test_bootstrap_rejects_invalid_organization_ids(organization_id: str):
    with pytest.raises(ValueError, match="organization_id"):
        await bootstrap_api_key(organization_id=organization_id)


async def test_bootstrap_api_key_creates_organization_and_one_hashed_admin_key():
    result = await bootstrap_api_key(
        organization_id="org_bootstrap",
        organization_slug="bootstrap",
        organization_name="Bootstrap tenant",
    )

    assert result.organization_created is True
    assert result.secret.startswith("vma_")
    assert result.prefix == result.secret[:12]

    async with session_scope() as db:
        organization = await db.get(Organization, "org_bootstrap")
        stored = await api_keys_q.get_api_key(
            db,
            result.key_id,
            organization_id="org_bootstrap",
        )

    assert organization is not None
    assert organization.slug == "bootstrap"
    assert stored is not None
    assert stored.scopes == [api_keys_q.API_SCOPE, api_keys_q.API_KEYS_MANAGE_SCOPE]
    assert stored.created_by == "bootstrap_api_key"
    assert stored.key_hash == api_keys_q.hash_api_key(result.secret)
    assert stored.key_hash != result.secret


async def test_bootstrap_refuses_duplicate_active_admin_without_explicit_override():
    await bootstrap_api_key(organization_id="org_bootstrap_guard")

    try:
        await bootstrap_api_key(organization_id="org_bootstrap_guard")
    except BootstrapConflict as exc:
        assert "already has an active management key" in str(exc)
    else:
        raise AssertionError("duplicate bootstrap unexpectedly succeeded")

    additional = await bootstrap_api_key(
        organization_id="org_bootstrap_guard",
        allow_additional_admin_key=True,
    )
    assert additional.organization_created is False

    async with session_scope() as db:
        count = await db.scalar(
            select(func.count()).select_from(ApiKey).where(
                ApiKey.organization_id == "org_bootstrap_guard"
            )
        )
    assert count == 2


async def test_bootstrap_with_preprovisioned_key_is_idempotent():
    supplied = "vma_preprovisioned_operator_key_for_staging"

    created = await bootstrap_api_key(
        organization_id="org_bootstrap_idempotent",
        api_key=supplied,
    )
    repeated = await bootstrap_api_key(
        organization_id="org_bootstrap_idempotent",
        api_key=supplied,
    )

    assert created.secret == supplied
    assert repeated.secret == supplied
    assert repeated.key_id == created.key_id
    assert repeated.organization_created is False

    async with session_scope() as db:
        count = await db.scalar(
            select(func.count()).select_from(ApiKey).where(
                ApiKey.organization_id == "org_bootstrap_idempotent"
            )
        )
    assert count == 1


async def test_bootstrap_rejects_invalid_preprovisioned_key():
    with pytest.raises(ValueError, match="vma_ prefix"):
        await bootstrap_api_key(
            organization_id="org_bootstrap_invalid_key",
            api_key="not-a-vma-key",
        )
