from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth import DatabaseApiKeyAuthProvider
from app.db.engine import session_scope
from app.db.models import Agent
from app.db.queries import api_keys as api_keys_q
from app.factory import create_app
from tests.conftest import TEST_HEADERS


async def test_database_api_key_provider_scopes_workspace():
    async with session_scope() as db:
        api_key, token = await api_keys_q.create_api_key(
            db,
            name="SDK test key",
            workspace_id="wrkspc_db_auth",
        )
        await db.commit()

    app = create_app(auth_provider=DatabaseApiKeyAuthProvider())
    headers = {**TEST_HEADERS, "x-api-key": token}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/v1/agents",
            headers=headers,
            json={"name": "DB Auth Agent", "model": {"id": "gpt-5.5"}},
        )

    assert response.status_code == 201, response.text
    agent_id = response.json()["id"]

    async with session_scope() as db:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one()
        refreshed_key = await api_keys_q.get_api_key_by_token(db, token)

    assert agent.workspace_id == "wrkspc_db_auth"
    assert refreshed_key is not None
    assert refreshed_key.id == api_key.id
    assert refreshed_key.last_used_at is not None


async def test_database_api_key_provider_rejects_archived_key():
    async with session_scope() as db:
        api_key, token = await api_keys_q.create_api_key(
            db,
            name="Archived key",
            workspace_id="wrkspc_archived",
        )
        await api_keys_q.archive_api_key(db, api_key)
        await db.commit()

    app = create_app(auth_provider=DatabaseApiKeyAuthProvider())
    headers = {**TEST_HEADERS, "authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/v1/agents", headers=headers)

    assert response.status_code == 401
