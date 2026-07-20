from sqlalchemy import select

from app.db.engine import session_scope
from app.db.models import UsageLedgerEntry
from app.db.queries import resources as res_q
from app.runtime.contracts import RuntimeResult
from app.runtime.deepagents_engine import _merge_usage
from app.runtime.runner import _model_usage_dimensions
from app.runtime.work_queue import WorkExecutionLease, execute_work_item
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
    work_id = ""

    async def fake_execute(
        version,
        history,
        *_args,
        admit_execution=None,
        organization_id=None,
        session_id=None,
        **_kwargs,
    ):
        if admit_execution is not None:
            await admit_execution()
        result = RuntimeResult(
            final_text="metered",
            run_state={
                "backend": "deepagents",
                "last_input_event_seq": max(
                    event.seq for event in history if event.type == "user.message"
                ),
            },
            usage={
                "input_tokens": 30,
                "output_tokens": 12,
                "total_tokens": 42,
                "input_token_details": {"cache_read": 9},
            },
        )
        async with session_scope() as db:
            work = await res_q.get_work_item_for_worker(db, work_id)
            assert work is not None
            lease_data = dict((work.data or {}).get("lease") or {})
            work_lease = WorkExecutionLease(
                work_id=work.id,
                worker_id=str(lease_data["worker_id"]),
                lease_id=str(lease_data["lease_id"]),
                generation=int(lease_data["generation"]),
                attempt=int((work.data or {}).get("attempt") or 0),
            )
        for _ in range(2):
            assert await runner._record_model_usage_after_result(
                session_id=str(session_id),
                organization_id=str(organization_id),
                effective_version=version,
                history=history,
                result=result,
                work_lease=work_lease,
            )
        return result

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
                        UsageLedgerEntry.organization_id == "org_test",
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
    assert entry.data["funding_source"] == "none"
    assert entry.data["accounting_phase"] == "postflight_actual"
    assert entry.dimensions == {
        "input_tokens": 30,
        "output_tokens": 12,
        "total_tokens": 42,
        "input_token_details": {"cache_read": 9},
    }
