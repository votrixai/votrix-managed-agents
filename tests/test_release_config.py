from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, call

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db.engine import _connect_args
from app.runtime.engine import _configure_checkpoint_schema, _postgres_dsn


ROOT = Path(__file__).resolve().parents[1]


def test_postgres_schema_is_applied_to_asyncpg_connections():
    settings = Settings(_env_file=None, database_schema="vma_rewrite_staging")

    args = _connect_args("postgresql+asyncpg://example/db", settings)

    assert args["server_settings"] == {"search_path": "vma_rewrite_staging"}


def test_postgres_schema_is_applied_to_langgraph_connections():
    dsn = _postgres_dsn(
        "postgresql+asyncpg://example/db?sslmode=require",
        "vma_rewrite_staging",
    )

    query = parse_qs(urlsplit(dsn).query)
    assert query["sslmode"] == ["require"]
    assert query["options"] == ["-csearch_path=vma_rewrite_staging"]


def test_checkpoint_database_url_is_explicitly_configurable():
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://pooler/db",
        vma_checkpoint_database_url="postgresql://direct/db",
    )

    assert settings.vma_checkpoint_database_url == "postgresql://direct/db"


@pytest.mark.asyncio
async def test_checkpoint_schema_is_set_after_connecting():
    connection = AsyncMock()
    cursor = AsyncMock()
    cursor.fetchone.return_value = {"current_schema": "vma_rewrite_production"}
    connection.execute.side_effect = [None, cursor]
    saver = SimpleNamespace(conn=connection)

    await _configure_checkpoint_schema(saver, "vma_rewrite_production")

    assert connection.execute.await_args_list == [
        call(
            "SELECT set_config('search_path', %s, false)",
            ("vma_rewrite_production",),
        ),
        call("SELECT current_schema() AS current_schema"),
    ]


@pytest.mark.asyncio
async def test_checkpoint_schema_configuration_fails_closed():
    connection = AsyncMock()
    cursor = AsyncMock()
    cursor.fetchone.return_value = {"current_schema": None}
    connection.execute.side_effect = [None, cursor]
    saver = SimpleNamespace(conn=connection)

    with pytest.raises(RuntimeError, match="did not select"):
        await _configure_checkpoint_schema(
            saver,
            "missing_checkpoint_schema",
        )


def test_runtime_manifests_use_direct_checkpoint_database_secrets():
    expected = {
        "service.production.yaml": "vma-database-url-direct",
        "service.worker.production.yaml": "vma-database-url-direct",
        "service.staging.yaml": "vma-database-url-direct-staging",
        "service.worker.staging.yaml": "vma-database-url-direct-staging",
    }

    for name, secret in expected.items():
        manifest = (ROOT / name).read_text(encoding="utf-8")
        assert (
            "- name: VMA_CHECKPOINT_DATABASE_URL\n"
            "              valueFrom:\n"
            "                secretKeyRef:\n"
            "                  key: latest\n"
            f"                  name: {secret}"
        ) in manifest


def test_migration_job_initializes_langgraph_checkpoints():
    script = (ROOT / "scripts/migrate.sh").read_text(encoding="utf-8")

    assert 'alembic upgrade "${ALEMBIC_TARGET:-head}"' in script
    assert "python -m app.runtime.checkpoint_setup" in script


def test_database_schema_rejects_sql_identifiers_that_need_quoting():
    with pytest.raises(ValidationError, match="valid PostgreSQL identifier"):
        Settings(_env_file=None, database_schema="rewrite;drop schema public")


def test_cloud_run_manifests_receive_the_full_git_commit():
    for name in (
        "service.production.yaml",
        "service.staging.yaml",
        "service.worker.production.yaml",
        "service.worker.staging.yaml",
    ):
        manifest = (ROOT / name).read_text(encoding="utf-8")
        assert "name: VMA_GIT_COMMIT_SHA" in manifest
        assert 'value: "__VMA_GIT_COMMIT_SHA__"' in manifest


def test_release_paths_replace_the_git_commit_placeholder():
    cloudbuild = (ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    assert "s|__VMA_GIT_COMMIT_SHA__|${COMMIT_SHA}|" in cloudbuild

    for name in (
        "scripts/gcloud/2-deploy-production.sh",
        "scripts/gcloud/3-deploy-staging.sh",
    ):
        script = (ROOT / name).read_text(encoding="utf-8")
        assert 'FULL_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD)' in script
        assert "s|__VMA_GIT_COMMIT_SHA__|${FULL_COMMIT}|" in script
