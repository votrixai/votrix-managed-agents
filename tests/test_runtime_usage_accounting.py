from sqlalchemy import select

from app.db.engine import session_scope
from app.db.models import UsageLedgerEntry
from app.db.queries import resources as res_q
from app.runtime.contracts import RuntimeResult
from app.runtime.deepagents_engine import _merge_usage
from app.runtime.runner import _model_usage_dimensions
from app.runtime.work_queue import execute_work_item
from tests.conftest import TEST_HEADERS


def test_model_usage_dimensions_preserve_nested_provider_breakdown():
    dimensions, total = _model_usage_dimensions(
        {
            "input_tokens": 12,
            "output_tokens": 8,
            "input_token_details": {"cache_read": 5, "cache_creation": 2},
            "output_token_details": {"reasoning": 3},
            "ignored_boolean": True,
        }
    )

    assert total == 20
    assert dimensions == {
        "input_tokens": 12,
        "output_tokens": 8,
        "total_tokens": 20,
        "input_token_details": {"cache_read": 5, "cache_creation": 2},
        "output_token_details": {"reasoning": 3},
    }


def test_stream_usage_merge_accumulates_nested_dimensions():
    total = {}
    _merge_usage(
        total,
        {
            "input_tokens": 10,
            "input_token_details": {"cache_read": 4},
        },
    )
    _merge_usage(
        total,
        {
            "output_tokens": 7,
            "input_token_details": {"cache_read": 3, "cache_creation": 2},
        },
    )

    assert total == {
        "input_tokens": 10,
        "output_tokens": 7,
        "input_token_details": {"cache_read": 7, "cache_creation": 2},
    }


async def test_worker_records_fenced_model_usage_once(client, monkeypatch):
    import app.runtime.runner as runner

    async def fake_execute(*_args, **_kwargs):
        return RuntimeResult(
            final_text="metered",
            usage={
                "input_tokens": 30,
                "output_tokens": 12,
                "total_tokens": 42,
                "input_token_details": {"cache_read": 9},
            },
        )

    monkeypatch.setattr(runner, "_execute", fake_execute)

    agent_response = await client.post(
        "/v1/agents",
        headers=TEST_HEADERS,
        json={"name": "Metered Agent", "model": {"id": "gpt-5.5"}},
    )
    assert agent_response.status_code == 201, agent_response.text
    environment_response = await client.post(
        "/v1/environments",
        headers=TEST_HEADERS,
        json={"name": "metered-self-hosted", "config": {"type": "self_hosted"}},
    )
    assert environment_response.status_code == 201, environment_response.text
    session_response = await client.post(
        "/v1/sessions",
        headers=TEST_HEADERS,
        json={
            "agent": {"id": agent_response.json()["id"], "version": 1},
            "environment_id": environment_response.json()["id"],
        },
    )
    assert session_response.status_code == 201, session_response.text
    session_id = session_response.json()["id"]

    events_response = await client.post(
        f"/v1/sessions/{session_id}/events",
        headers=TEST_HEADERS,
        json={"events": [{"type": "user.message", "content": "meter this turn"}]},
    )
    assert events_response.status_code == 200, events_response.text

    async with session_scope() as db:
        work = (
            await res_q.list_resources(
                db,
                resource_type="environment_work",
                parent_id=environment_response.json()["id"],
                limit=10,
            )
        )[0]
        work_id = work.id

    assert await execute_work_item(work_id, worker_id="usage-worker") == "completed"

    async with session_scope() as db:
        entries = list(
            (
                await db.execute(
                    select(UsageLedgerEntry).where(
                        UsageLedgerEntry.workspace_id == "wrkspc_default",
                        UsageLedgerEntry.metric == "model_tokens",
                    )
                )
            ).scalars()
        )

    assert len(entries) == 1
    entry = entries[0]
    assert entry.quantity == 42
    assert entry.unit == "token"
    assert entry.model == "gpt-5.5"
    assert entry.source_type == "session"
    assert entry.source_id == session_id
    assert entry.idempotency_key == f"model_tokens:{work_id}"
    assert entry.dimensions == {
        "input_tokens": 30,
        "output_tokens": 12,
        "total_tokens": 42,
        "input_token_details": {"cache_read": 9},
    }
