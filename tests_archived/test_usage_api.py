from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.engine import session_scope
from app.db.queries import governance as governance_q
from tests.conftest import TEST_HEADERS

UTC = timezone.utc


async def _headers(database_api_key_factory, organization_id: str) -> dict[str, str]:
    token = f"key-{organization_id}"
    await database_api_key_factory(token=token, organization_id=organization_id)
    return {**TEST_HEADERS, "x-api-key": token}


async def _append(
    *,
    organization_id: str,
    metric: str,
    quantity: int,
    unit: str,
    source_type: str,
    source_id: str,
    occurred_at: datetime,
):
    async with session_scope() as db:
        entry = await governance_q.append_usage_entry(
            db,
            organization_id=organization_id,
            metric=metric,
            quantity=quantity,
            unit=unit,
            provider="openrouter" if metric == "model_tokens" else None,
            model="deepseek/deepseek-v4-pro" if metric == "model_tokens" else None,
            source_type=source_type,
            source_id=source_id,
            dimensions={"input_tokens": quantity} if metric == "model_tokens" else {},
            data={"accounting_phase": "postflight_actual"},
            occurred_at=occurred_at,
        )
        await db.commit()
        return entry


async def test_usage_api_is_organization_scoped_and_exposes_only_raw_facts(
    client,
    database_api_key_factory,
) -> None:
    headers_a = await _headers(database_api_key_factory, "org_usage_a")
    await _headers(database_api_key_factory, "org_usage_b")
    instant = datetime(2026, 7, 16, 14, 0, tzinfo=UTC)
    own = await _append(
        organization_id="org_usage_a",
        metric="model_tokens",
        quantity=17,
        unit="token",
        source_type="session",
        source_id="sess_a",
        occurred_at=instant,
    )
    await _append(
        organization_id="org_usage_b",
        metric="model_tokens",
        quantity=999,
        unit="token",
        source_type="session",
        source_id="sess_b",
        occurred_at=instant,
    )

    response = await client.get("/v1/usage", headers=headers_a)

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["data"]] == [own.id]
    item = body["data"][0]
    assert item == {
        "id": own.id,
        "type": "usage",
        "organization_id": "org_usage_a",
        "metric": "model_tokens",
        "quantity": 17,
        "unit": "token",
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-pro",
        "source_type": "session",
        "source_id": "sess_a",
        "dimensions": {"input_tokens": 17},
        "data": {"accounting_phase": "postflight_actual"},
        "occurred_at": instant.isoformat().replace("+00:00", "Z"),
    }
    assert not {
        "user_id",
        "user_profile_id",
        "end_user_id",
        "external_user_id",
        "idempotency_key",
        "model_cost",
        "total_cost",
    }.intersection(item)
    assert "totals" not in body


async def test_usage_api_filters_by_session_metric_and_time(
    client,
    database_api_key_factory,
) -> None:
    headers = await _headers(database_api_key_factory, "org_usage_filters")
    start = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
    expected = await _append(
        organization_id="org_usage_filters",
        metric="model_tokens",
        quantity=10,
        unit="token",
        source_type="session",
        source_id="sess_target",
        occurred_at=start + timedelta(minutes=1),
    )
    await _append(
        organization_id="org_usage_filters",
        metric="sandbox_seconds",
        quantity=20,
        unit="second",
        source_type="session",
        source_id="sess_target",
        occurred_at=start + timedelta(minutes=2),
    )
    await _append(
        organization_id="org_usage_filters",
        metric="model_tokens",
        quantity=30,
        unit="token",
        source_type="session",
        source_id="sess_other",
        occurred_at=start + timedelta(minutes=2),
    )
    await _append(
        organization_id="org_usage_filters",
        metric="model_tokens",
        quantity=40,
        unit="token",
        source_type="sandbox",
        source_id="sess_target",
        occurred_at=start + timedelta(minutes=2),
    )
    await _append(
        organization_id="org_usage_filters",
        metric="model_tokens",
        quantity=50,
        unit="token",
        source_type="session",
        source_id="sess_target",
        occurred_at=start + timedelta(minutes=3),
    )

    response = await client.get(
        "/v1/usage",
        headers=headers,
        params={
            "session_id": "sess_target",
            "metric": "model_tokens",
            "occurred_at[gte]": start.isoformat(),
            "occurred_at[lt]": (start + timedelta(minutes=3)).isoformat(),
        },
    )

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["data"]] == [expected.id]


async def test_usage_api_uses_filter_bound_database_cursors(
    client,
    database_api_key_factory,
) -> None:
    headers = await _headers(database_api_key_factory, "org_usage_pages")
    instant = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    for quantity in (1, 2, 3):
        await _append(
            organization_id="org_usage_pages",
            metric="model_tokens",
            quantity=quantity,
            unit="token",
            source_type="session",
            source_id="sess_page",
            occurred_at=instant,
        )

    seen_ids: list[str] = []
    page: str | None = None
    first_cursor: str | None = None
    for index in range(3):
        params = {"limit": 1, "session_id": "sess_page"}
        if page is not None:
            params["page"] = page
        response = await client.get("/v1/usage", headers=headers, params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        seen_ids.append(body["data"][0]["id"])
        page = body["next_page"]
        if index == 0:
            first_cursor = page
            assert body["has_more"] is True
            assert isinstance(page, str) and page.startswith("usage_")

    assert len(set(seen_ids)) == 3
    assert page is None

    mismatched = await client.get(
        "/v1/usage",
        headers=headers,
        params={
            "limit": 1,
            "session_id": "sess_page",
            "metric": "sandbox_seconds",
            "page": first_cursor,
        },
    )
    assert mismatched.status_code == 400
    assert mismatched.json()["error"]["message"] == "Invalid usage page cursor"
