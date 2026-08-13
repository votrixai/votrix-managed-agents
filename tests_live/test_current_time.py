from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio
from dotenv import dotenv_values

from tests_live.helpers import calls, run_turn, said

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def api(server) -> AsyncIterator[httpx.AsyncClient]:
    api_key = os.environ.get("VMA_TEST_API_KEY") or dotenv_values(".env").get(
        "VMA_TEST_API_KEY"
    )

    if not api_key:
        pytest.skip("请在 .env 中配置 VMA_TEST_API_KEY")

    async with httpx.AsyncClient(
        base_url=server,
        headers={"x-api-key": api_key},
        timeout=httpx.Timeout(300.0, connect=10.0),
    ) as client:
        yield client


async def test_model_knows_the_current_utc_date(api, session):
    date_before_request = datetime.now(timezone.utc).date().isoformat()

    events = await run_turn(
        api,
        session,
        "Do not use any tools. What is the current UTC date you were given "
        "in your system instructions? Reply with only the date in YYYY-MM-DD format.",
    )

    date_after_request = datetime.now(timezone.utc).date().isoformat()
    answer = said(events).strip()

    assert answer in {date_before_request, date_after_request}
    assert calls(events) == []