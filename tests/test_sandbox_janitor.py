from contextlib import asynccontextmanager

from app.runtime import sandbox_lifecycle


async def test_sandbox_janitor_cleanup_is_unchanged_off_postgres(monkeypatch):
    calls: list[int] = []

    async def fake_cleanup(*, limit: int = 25) -> int:
        calls.append(limit)
        return 4

    monkeypatch.setattr(
        sandbox_lifecycle,
        "_cleanup_expired_session_sandboxes",
        fake_cleanup,
    )

    assert await sandbox_lifecycle.cleanup_expired_session_sandboxes(limit=7) == 4
    assert calls == [7]


async def test_postgres_janitor_holds_lock_on_session_scoped_connection(monkeypatch):
    statements: list[str] = []

    class Result:
        def __init__(self, value=None) -> None:
            self.value = value

        def scalar(self):
            return self.value

    class Connection:
        async def execute(self, statement, _parameters):
            rendered = str(statement)
            statements.append(rendered)
            return Result(True if "pg_try_advisory_lock" in rendered else None)

    @asynccontextmanager
    async def fake_session_scoped_connection():
        yield Connection()

    class Engine:
        class Dialect:
            name = "postgresql"

        dialect = Dialect()

    monkeypatch.setattr(sandbox_lifecycle, "get_engine", lambda: Engine())
    monkeypatch.setattr(
        sandbox_lifecycle,
        "session_scoped_connection",
        fake_session_scoped_connection,
    )

    async def fake_cleanup(**_kwargs):
        return 3

    monkeypatch.setattr(
        sandbox_lifecycle,
        "_cleanup_expired_session_sandboxes",
        fake_cleanup,
    )

    assert await sandbox_lifecycle.cleanup_expired_session_sandboxes(limit=9) == 3
    assert len(statements) == 2
    assert "pg_try_advisory_lock" in statements[0]
    assert "pg_advisory_unlock" in statements[1]
