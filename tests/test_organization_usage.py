"""Provider-authoritative Organization usage aggregation contracts."""

from __future__ import annotations

from decimal import Decimal

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db.queries import accounts as accounts_q
from app.main import app
from app.routers.deps import get_db
from app.services import accounts as accounts_service
from tests.conftest import FakeKeys


@pytest_asyncio.fixture
async def client(db):
    app.dependency_overrides[get_db] = lambda: db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def _usage_for_accounts(db, org, values: list[str]) -> FakeKeys:
    keys = FakeKeys()
    accounts = await accounts_q.list_all_accounts(db, organization_id=org)
    assert len(accounts) == len(values)
    for account, value in zip(accounts, values, strict=True):
        assert account.credential is not None
        keys.usage[account.credential.key_hash] = Decimal(value)
    return keys


async def test_organization_usage_sums_every_account(db, org):
    keys = FakeKeys()
    await accounts_service.create_account(
        db,
        organization_id=org,
        name="Production",
        keys=keys,
    )
    usage_keys = await _usage_for_accounts(db, org, ["1.25", "2.75"])

    usage = await accounts_service.get_organization_usage(
        db,
        organization_id=org,
        keys=usage_keys,
    )

    assert usage.usage_usd == Decimal("4.00")
    assert [row.usage_usd for row in usage.accounts] == [
        Decimal("1.25"),
        Decimal("2.75"),
    ]
    assert usage.accounts[0].is_default is True


async def test_organization_usage_is_a_real_http_route(
    client,
    db,
    org,
    headers,
    monkeypatch,
):
    usage_keys = await _usage_for_accounts(db, org, ["1.50"])
    monkeypatch.setattr(accounts_service, "_key_admin", lambda: usage_keys)

    response = await client.get("/v1/accounts/usage", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["usage_usd"] == "1.50"
