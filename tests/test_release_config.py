from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db.engine import _connect_args
from app.runtime.engine import _postgres_dsn


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
