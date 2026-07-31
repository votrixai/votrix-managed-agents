from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db.engine import _connect_args
from app.runtime.engine import _postgres_dsn


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
