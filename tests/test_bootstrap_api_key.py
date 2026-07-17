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
    assert result.secret.startswith(api_keys_q.TEST_API_KEY_PREFIX)
    assert result.prefix == result.secret[: api_keys_q.DISPLAYED_API_KEY_PREFIX_LENGTH]

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
    supplied = "vma_test_preprovisioned_operator_key_for_staging"

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
    with pytest.raises(ValueError, match="vma_live_ or vma_test_ prefix"):
        await bootstrap_api_key(
            organization_id="org_bootstrap_invalid_key",
            api_key="not-a-vma-key",
        )


@pytest.mark.parametrize(
    ("app_env", "expected_prefix"),
    [
        ("production", api_keys_q.LIVE_API_KEY_PREFIX),
        ("PRODUCTION", api_keys_q.LIVE_API_KEY_PREFIX),
        ("staging", api_keys_q.TEST_API_KEY_PREFIX),
        ("test", api_keys_q.TEST_API_KEY_PREFIX),
        ("local", api_keys_q.TEST_API_KEY_PREFIX),
        ("development", api_keys_q.TEST_API_KEY_PREFIX),
        ("unknown", api_keys_q.TEST_API_KEY_PREFIX),
    ],
)
def test_generated_api_key_prefix_is_environment_aware(app_env: str, expected_prefix: str):
    secret = api_keys_q.generate_api_key(app_env=app_env)

    assert secret.startswith(expected_prefix)
    assert len(secret) > len(expected_prefix)


def test_environment_specific_supplied_key_must_match_target():
    with pytest.raises(ValueError, match="vma_live_"):
        api_keys_q.validate_api_key_prefix(
            "vma_test_wrong_environment",
            app_env="production",
        )
    with pytest.raises(ValueError, match="vma_test_"):
        api_keys_q.validate_api_key_prefix(
            "vma_live_wrong_environment",
            app_env="staging",
        )


def test_supplied_key_requires_sufficient_random_material():
    with pytest.raises(ValueError, match="at least 32 characters"):
        api_keys_q.validate_api_key_prefix(
            "vma_test_short",
            app_env="test",
        )


async def test_legacy_supplied_key_only_reuses_matching_active_admin():
    legacy_key = "vma_legacy_operator_key_created_before_prefix_contract"
    async with session_scope() as db:
        db.add(
            Organization(
                id="org_bootstrap_legacy",
                slug="bootstrap-legacy",
                name="Bootstrap legacy",
                metadata_={},
            )
        )
        existing, _ = await api_keys_q.create_api_key(
            db,
            organization_id="org_bootstrap_legacy",
            name="Legacy bootstrap admin",
            token=legacy_key,
            scopes=[api_keys_q.API_SCOPE, api_keys_q.API_KEYS_MANAGE_SCOPE],
        )
        existing_id = existing.id
        await db.commit()

    result = await bootstrap_api_key(
        organization_id="org_bootstrap_legacy",
        organization_slug="bootstrap-legacy",
        organization_name="Bootstrap legacy",
        api_key=legacy_key,
    )

    assert result.key_id == existing_id
    assert result.secret == legacy_key
    assert result.organization_created is False


async def test_legacy_supplied_key_cannot_create_a_new_admin():
    with pytest.raises(ValueError, match="only reuse an existing active management key"):
        await bootstrap_api_key(
            organization_id="org_bootstrap_new_legacy",
            api_key="vma_legacy_key_must_not_be_created",
        )
