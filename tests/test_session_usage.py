from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.models.errors import NotFound, UsageUnavailable
from app.services import sessions as service
from app.utils.openrouter_analytics import (
    OpenRouterAnalyticsClient,
    OpenRouterAnalyticsError,
    OpenRouterSessionUsage,
)


def _meta() -> dict:
    return {
        "data": {
            "dimensions": [
                {"name": "session", "display_label": "Session"},
                {"name": "api_key_id", "display_label": "API Key"},
            ],
            "metrics": [
                {
                    "name": "total_usage",
                    "display_label": "Total Usage",
                    "display_format": "currency",
                }
            ],
            "operators": [{"name": "eq", "value_type": "scalar"}],
            "granularities": [],
        }
    }


async def test_openrouter_query_is_scoped_to_session_and_account_key():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/analytics/meta"):
            return httpx.Response(200, json=_meta())
        return httpx.Response(
            200,
            json={
                "data": {
                    "data": [{"total_usage": "0.0842"}],
                    "metadata": {"row_count": 1, "truncated": False},
                }
            },
        )

    now = datetime(2026, 8, 22, 12, tzinfo=UTC)
    client = OpenRouterAnalyticsClient(
        "management-secret", transport=httpx.MockTransport(handler)
    )

    usage = await client.get_session_usage(
        session_id="sess_one",
        api_key_hash="abc123",
        started_at=now - timedelta(hours=1),
        as_of=now,
    )

    assert usage == OpenRouterSessionUsage(usage_usd=Decimal("0.0842"), as_of=now)
    assert len(requests) == 2
    assert requests[0].headers["authorization"] == "Bearer management-secret"
    body = json.loads(requests[1].content)
    assert body["metrics"] == ["total_usage"]
    assert body["filters"] == [
        {"field": "session", "operator": "eq", "value": "sess_one"},
        {"field": "api_key_id", "operator": "eq", "value": "abc123"},
    ]
    assert body["time_range"] == {
        "start": "2026-08-22T11:00:00Z",
        "end": "2026-08-22T12:00:00Z",
    }


async def test_long_session_is_summed_across_provider_query_windows():
    query_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_count
        if request.url.path.endswith("/analytics/meta"):
            return httpx.Response(200, json=_meta())
        query_count += 1
        return httpx.Response(
            200,
            json={
                "data": {
                    "data": [{"total_usage": query_count}],
                    "metadata": {"row_count": 1, "truncated": False},
                }
            },
        )

    now = datetime(2026, 8, 22, 12, tzinfo=UTC)
    usage = await OpenRouterAnalyticsClient(
        "management-secret", transport=httpx.MockTransport(handler)
    ).get_session_usage(
        session_id="sess_old",
        api_key_hash="abc123",
        started_at=now - timedelta(days=31),
        as_of=now,
    )

    assert query_count == 2
    assert usage.usage_usd == Decimal("3")


async def test_truncated_provider_result_is_never_treated_as_a_billable_total():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/analytics/meta"):
            return httpx.Response(200, json=_meta())
        return httpx.Response(
            200,
            json={
                "data": {
                    "data": [{"total_usage": "1"}],
                    "metadata": {"row_count": 1, "truncated": True},
                }
            },
        )

    with pytest.raises(OpenRouterAnalyticsError, match="truncated"):
        await OpenRouterAnalyticsClient(
            "management-secret", transport=httpx.MockTransport(handler)
        ).get_session_usage(
            session_id="sess_one",
            api_key_hash="abc123",
            started_at=datetime(2026, 8, 22, tzinfo=UTC),
            as_of=datetime(2026, 8, 22, 1, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "query_data",
    [
        {"data": [{"total_usage": "1"}], "metadata": {"row_count": 1}},
        {
            "data": [{}],
            "metadata": {"row_count": 1, "truncated": False},
        },
    ],
)
async def test_incomplete_analytics_response_is_never_treated_as_zero(query_data):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/analytics/meta"):
            return httpx.Response(200, json=_meta())
        return httpx.Response(200, json={"data": query_data})

    with pytest.raises(OpenRouterAnalyticsError):
        await OpenRouterAnalyticsClient(
            "management-secret", transport=httpx.MockTransport(handler)
        ).get_session_usage(
            session_id="sess_one",
            api_key_hash="abc123",
            started_at=datetime(2026, 8, 22, tzinfo=UTC),
            as_of=datetime(2026, 8, 22, 1, tzinfo=UTC),
        )


class FakeAnalytics:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    async def get_session_usage(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return OpenRouterSessionUsage(
            usage_usd=Decimal("2.75"),
            as_of=datetime(2026, 8, 22, 12, tzinfo=UTC),
        )


async def test_service_reads_historical_session_from_its_default_account(db, org, session):
    analytics = FakeAnalytics()
    session_id = session.id

    usage = await service.get_session_usage(
        db,
        session_id=session_id,
        organization_id=org,
        analytics=analytics,
    )

    assert usage.usage_usd == Decimal("2.75")
    assert usage.account_id.startswith("acct_")
    assert analytics.calls[0]["session_id"] == session_id
    assert analytics.calls[0]["api_key_hash"].startswith("hash-")


async def test_session_usage_cannot_cross_organization_boundary(
    db, session, other_tenant
):
    other_org, _ = other_tenant
    analytics = FakeAnalytics()

    with pytest.raises(NotFound):
        await service.get_session_usage(
            db,
            session_id=session.id,
            organization_id=other_org,
            analytics=analytics,
        )

    assert analytics.calls == []


async def test_provider_failure_becomes_usage_unavailable(db, org, session):
    analytics = FakeAnalytics(error=OpenRouterAnalyticsError("provider detail"))

    with pytest.raises(UsageUnavailable, match="complete Session usage snapshot"):
        await service.get_session_usage(
            db,
            session_id=session.id,
            organization_id=org,
            analytics=analytics,
        )
