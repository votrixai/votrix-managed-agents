from types import SimpleNamespace

from app.runtime import runner


async def test_runtime_history_reads_past_the_first_500_events(monkeypatch):
    events = [SimpleNamespace(seq=seq) for seq in range(1, 1002)]
    requested_after: list[int] = []

    async def fake_list_events(
        _db,
        *,
        session_id: str,
        after_seq: int,
        limit: int,
        workspace_id: str,
    ):
        assert session_id == "sess_history"
        assert workspace_id == "wrkspc_history"
        assert limit == 500
        requested_after.append(after_seq)
        return [event for event in events if event.seq > after_seq][:limit]

    monkeypatch.setattr(runner.events_q, "list_events", fake_list_events)

    history = await runner._list_runtime_history(
        object(),
        session_id="sess_history",
        workspace_id="wrkspc_history",
    )

    assert [event.seq for event in history] == list(range(1, 1002))
    assert requested_after == [0, 500, 1000]
