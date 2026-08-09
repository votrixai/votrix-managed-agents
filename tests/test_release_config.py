from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, call

import pytest
import yaml
from pydantic import ValidationError

from app.config import Settings
from app.db.engine import _connect_args
from app.runtime.engine import _configure_checkpoint_connection, _postgres_dsn


ROOT = Path(__file__).resolve().parents[1]


def test_cloud_run_manifests_have_unique_environment_variable_names():
    for manifest_name in (
        "service.production.yaml",
        "service.staging.yaml",
        "service.worker.production.yaml",
        "service.worker.staging.yaml",
    ):
        manifest = yaml.safe_load(
            (ROOT / manifest_name).read_text(encoding="utf-8")
        )
        for container in manifest["spec"]["template"]["spec"]["containers"]:
            names = [entry["name"] for entry in container.get("env", [])]
            duplicates = sorted(
                name for name in set(names) if names.count(name) > 1
            )
            assert not duplicates, (
                f"{manifest_name} contains duplicate environment variables: "
                f"{duplicates}"
            )


def test_firecrawl_secret_is_imported_and_injected_into_every_runtime():
    expected = {
        "service.production.yaml": "vma-firecrawl-api-key",
        "service.worker.production.yaml": "vma-firecrawl-api-key",
        "service.staging.yaml": "vma-firecrawl-api-key-staging",
        "service.worker.staging.yaml": "vma-firecrawl-api-key-staging",
    }

    importer = (ROOT / "scripts/gcloud/1-create-secrets.sh").read_text(
        encoding="utf-8"
    )
    assert "FIRECRAWL_API_KEY|vma-firecrawl-api-key" in importer

    preflight = (ROOT / "scripts/gcloud/preflight.sh").read_text(encoding="utf-8")
    assert "firecrawl-api-key" in preflight

    for manifest_name, secret_name in expected.items():
        manifest = yaml.safe_load(
            (ROOT / manifest_name).read_text(encoding="utf-8")
        )
        env = manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        firecrawl = next(entry for entry in env if entry["name"] == "FIRECRAWL_API_KEY")
        assert firecrawl["valueFrom"]["secretKeyRef"] == {
            "key": "latest",
            "name": secret_name,
        }


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
async def test_checkpoint_schema_is_set_on_every_pooled_connection(monkeypatch):
    """Per connection, not once per saver.

    The checkpointer borrows from a pool now, so there is no one connection to
    configure — each new one has to be pointed at the schema as it is opened.
    """
    monkeypatch.setattr(
        "app.runtime.engine.get_settings",
        lambda: SimpleNamespace(database_schema="vma_rewrite_production"),
    )
    connection = AsyncMock()
    cursor = AsyncMock()
    cursor.fetchone.return_value = {"current_schema": "vma_rewrite_production"}
    connection.execute.side_effect = [None, cursor]

    await _configure_checkpoint_connection(connection)

    assert connection.execute.await_args_list == [
        call(
            "SELECT set_config('search_path', %s, false)",
            ("vma_rewrite_production",),
        ),
        call("SELECT current_schema() AS current_schema"),
    ]


@pytest.mark.asyncio
async def test_checkpoint_schema_configuration_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "app.runtime.engine.get_settings",
        lambda: SimpleNamespace(database_schema="missing_checkpoint_schema"),
    )
    connection = AsyncMock()
    cursor = AsyncMock()
    cursor.fetchone.return_value = {"current_schema": None}
    connection.execute.side_effect = [None, cursor]

    with pytest.raises(RuntimeError, match="did not select"):
        await _configure_checkpoint_connection(connection)


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


def test_runtime_manifests_use_the_actual_sandbox_timeout_setting():
    expected = {
        "service.staging.yaml": "300",
        "service.worker.staging.yaml": "300",
        "service.production.yaml": "900",
        "service.worker.production.yaml": "900",
    }

    for name, timeout in expected.items():
        manifest = (ROOT / name).read_text(encoding="utf-8")
        assert (
            "- name: SANDBOX_TIMEOUT_SECONDS\n"
            f'              value: "{timeout}"'
        ) in manifest
        assert "VMA_E2B_TIMEOUT_SECONDS" not in manifest


def test_migration_job_runs_alembic():
    script = (ROOT / "scripts/migrate.sh").read_text(encoding="utf-8")

    assert 'alembic upgrade "${ALEMBIC_TARGET:-head}"' in script


def test_operator_bootstrap_targets_the_deployed_database_schema():
    script = (ROOT / "scripts/gcloud/6-bootstrap-operator.sh").read_text(
        encoding="utf-8"
    )

    assert 'DEFAULT_DATABASE_SCHEMA="vma_rewrite_staging"' in script
    assert 'DEFAULT_DATABASE_SCHEMA="vma_rewrite_production"' in script
    assert "DATABASE_SCHEMA=${VMA_BOOTSTRAP_DATABASE_SCHEMA:-$DEFAULT_DATABASE_SCHEMA}" in script
    assert "export DATABASE_SCHEMA" in script


def test_operator_bootstrap_pipelines_fail_closed_and_use_enabled_version():
    script = (ROOT / "scripts/gcloud/6-bootstrap-operator.sh").read_text(
        encoding="utf-8"
    )

    assert script.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in script
    assert 'gcloud secrets versions access "$ENABLED_VERSION"' in script


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
