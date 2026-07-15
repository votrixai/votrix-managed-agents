from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import AuditLedgerEntry
from sqlalchemy import func, select

from tests.conftest import TEST_HEADERS


async def test_workspace_request_quota_returns_429_and_retry_metadata(
    client,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VMA_REQUESTS_PER_MINUTE", "2")
    get_settings.cache_clear()

    first = await client.get("/v1/agents", headers=TEST_HEADERS)
    second = await client.get("/v1/agents", headers=TEST_HEADERS)
    denied = await client.get("/v1/agents", headers=TEST_HEADERS)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert denied.status_code == 429, denied.text
    assert first.headers["x-ratelimit-limit"] == "2"
    assert first.headers["x-ratelimit-remaining"] == "1"
    assert second.headers["x-ratelimit-remaining"] == "0"
    assert denied.headers["retry-after"]
    assert denied.headers["x-ratelimit-limit"] == "2"
    assert denied.headers["x-ratelimit-remaining"] == "0"
    assert denied.json()["error"]["code"] == "request_quota_exceeded"
    assert denied.json()["request_id"] == denied.headers["request-id"]

    async with session_scope() as db:
        denied_audits = await db.scalar(
            select(func.count())
            .select_from(AuditLedgerEntry)
            .where(
                AuditLedgerEntry.action == "api.request.authorize",
                AuditLedgerEntry.outcome == "denied",
            )
        )
        completion_audits = await db.scalar(
            select(func.count())
            .select_from(AuditLedgerEntry)
            .where(AuditLedgerEntry.action == "api.request.complete")
        )
    assert denied_audits == 1
    assert completion_audits == 3
