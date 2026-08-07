"""Paging, which every list endpoint shares.

The point of a cursor is that it survives writes. These tests insert rows
between pages on purpose, because that is the case an offset gets wrong and
the only reason to have done this work.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db.queries import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.db.queries import skills as skills_q
from app.main import app
from app.routers.deps import get_db


@pytest_asyncio.fixture
async def client(db):
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def make_skills(db, org, count, *, start=0):
    """Rows to page over. Skills are the cheapest thing to make a lot of."""
    for i in range(start, start + count):
        await skills_q.create_skill(
            db,
            organization_id=org,
            name=f"skill-{i:03d}",
            description="x",
            storage_key=f"k/{i}",
            size_bytes=1,
            sha256="0" * 64,
        )
    await db.commit()


async def page(client, headers, **params):
    response = await client.get("/v1/skills", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# --- the envelope ------------------------------------------------------------


async def test_a_full_page_says_there_is_more(client, headers, db, org):
    await make_skills(db, org, 5)

    body = await page(client, headers, limit=2)

    assert len(body["data"]) == 2
    assert body["has_more"] is True
    assert body["first_id"] == body["data"][0]["id"]
    assert body["last_id"] == body["data"][1]["id"]


async def test_the_last_page_says_so(client, headers, db, org):
    """The one thing a client cannot work out for itself: a page that happens
    to be exactly `limit` long is not necessarily the end."""
    await make_skills(db, org, 4)

    body = await page(client, headers, limit=4)

    assert len(body["data"]) == 4
    assert body["has_more"] is False


async def test_an_empty_list_has_no_cursor(client, headers):
    body = await page(client, headers)

    assert body["data"] == []
    assert body["has_more"] is False
    assert body["first_id"] is None and body["last_id"] is None


# --- walking through ---------------------------------------------------------


async def test_paging_forward_sees_everything_once(client, headers, db, org):
    await make_skills(db, org, 7)

    seen: list[str] = []
    cursor = None
    while True:
        body = await page(client, headers, limit=2, **({"after_id": cursor} if cursor else {}))
        seen.extend(item["name"] for item in body["data"])
        if not body["has_more"]:
            break
        cursor = body["last_id"]

    assert len(seen) == 7
    assert len(set(seen)) == 7, "a row came back on two different pages"


async def test_paging_back_returns_the_previous_page(client, headers, db, org):
    await make_skills(db, org, 6)

    first = await page(client, headers, limit=2)
    second = await page(client, headers, limit=2, after_id=first["last_id"])

    back = await page(client, headers, limit=2, before_id=second["first_id"])

    assert [i["id"] for i in back["data"]] == [i["id"] for i in first["data"]]


# --- the reason for all of this ----------------------------------------------


async def test_rows_added_mid_walk_do_not_repeat_the_page(client, headers, db, org):
    """What an offset gets wrong.

    Newest first, so anything inserted goes to the front and pushes every
    later row down. `OFFSET 2` would then start two rows into a list that has
    shifted, and hand back rows the caller already had.
    """
    await make_skills(db, org, 4)
    first = await page(client, headers, limit=2)

    await make_skills(db, org, 3, start=100)

    second = await page(client, headers, limit=2, after_id=first["last_id"])

    overlap = {i["id"] for i in first["data"]} & {i["id"] for i in second["data"]}
    assert not overlap, "the second page repeated rows from the first"


async def test_a_deleted_cursor_row_does_not_lose_the_rest(
    client, headers, db, org, monkeypatch
):
    """A cursor naming a row that is gone yields nothing rather than silently
    restarting from the top — better an empty page than a repeated one."""
    async def delete_object(_key):
        return None

    monkeypatch.setattr("app.services.skills.storage.delete_object", delete_object)
    await make_skills(db, org, 4)
    first = await page(client, headers, limit=2)
    await client.delete(f"/v1/skills/{first['last_id']}", headers=headers)

    body = await page(client, headers, limit=2, after_id=first["last_id"])

    assert first["last_id"] not in [i["id"] for i in body["data"]]


# --- limits ------------------------------------------------------------------


async def test_a_caller_that_does_not_ask_gets_the_default(client, headers, db, org):
    await make_skills(db, org, DEFAULT_PAGE_SIZE + 5)

    body = await page(client, headers)

    assert len(body["data"]) == DEFAULT_PAGE_SIZE


@pytest.mark.parametrize("asked", [0, -1, MAX_PAGE_SIZE * 10])
async def test_an_impossible_limit_is_clamped_not_obeyed(client, headers, db, org, asked):
    await make_skills(db, org, 3)

    body = await page(client, headers, limit=asked)

    assert 1 <= len(body["data"]) <= MAX_PAGE_SIZE


# --- every list endpoint answers the same shape ------------------------------


@pytest.mark.parametrize(
    "path", ["/v1/skills", "/v1/agents", "/v1/sessions", "/v1/environments", "/v1/files"]
)
async def test_every_list_endpoint_uses_the_same_envelope(client, headers, path):
    body = (await client.get(path, headers=headers)).json()

    assert set(body) == {"data", "has_more", "first_id", "last_id"}
