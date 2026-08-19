import pytest

from scripts.rename_legacy_schema import schema_rename_action


@pytest.mark.parametrize("app_env", ["local", "development", ""])
def test_schema_rename_skips_non_hosted_environments(app_env):
    assert (
        schema_rename_action(
            app_env=app_env,
            configured_schema="vma",
            existing_schemas={"vma_rewrite_production"},
        )
        == "skip"
    )


def test_schema_rename_requires_the_new_configured_name():
    assert (
        schema_rename_action(
            app_env="production",
            configured_schema="vma_rewrite_production",
            existing_schemas={"vma_rewrite_production"},
        )
        == "skip"
    )


@pytest.mark.parametrize(
    ("app_env", "legacy_schema"),
    [
        ("staging", "vma_rewrite_staging"),
        ("production", "vma_rewrite_production"),
    ],
)
def test_schema_rename_moves_the_known_legacy_schema(app_env, legacy_schema):
    assert (
        schema_rename_action(
            app_env=app_env,
            configured_schema="vma",
            existing_schemas={legacy_schema},
        )
        == "rename"
    )


def test_schema_rename_is_idempotent_after_cutover():
    assert (
        schema_rename_action(
            app_env="production",
            configured_schema="vma",
            existing_schemas={"vma"},
        )
        == "already"
    )


def test_schema_rename_allows_fresh_database_initialization():
    assert (
        schema_rename_action(
            app_env="staging",
            configured_schema="vma",
            existing_schemas=set(),
        )
        == "fresh"
    )


def test_schema_rename_fails_when_both_names_exist():
    with pytest.raises(RuntimeError, match="both .* exist"):
        schema_rename_action(
            app_env="production",
            configured_schema="vma",
            existing_schemas={"vma_rewrite_production", "vma"},
        )
