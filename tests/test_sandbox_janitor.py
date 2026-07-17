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
