"""Real fixtures: a real database, a real bucket, a real sandbox, a real model.

This is the opposite of `tests/conftest.py` in every respect, which is why the
two suites live apart. Nothing here is stubbed except `web_search` — see
`stubs.py` for why that one has to be.

Everything runs on one event loop for the whole session, because the server is
a fixture: uvicorn is started in this interpreter so the `web_search` patch
reaches the code the server runs, and so SSE has a real socket to arrive on.
Test modules opt in with `pytestmark = pytest.mark.asyncio(loop_scope="session")`.
"""

from __future__ import annotations

import asyncio
import io
import socket
import uuid
import zipfile
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
import uvicorn

FIXTURES = Path(__file__).parent / "fixtures"
UPLOAD_NAMES = ("revenue.csv", "notes.txt", "config.json", "readme.md")

# A turn is a real model doing real work in a real container, and `inline`
# dispatch means the request stays open for all of it.
TURN_TIMEOUT_SECONDS = 600.0

# `task` and `write_todos` are ruled out for the same reason and it is not
# danger: both add tool calls nobody asked for. `task` hides work in a
# sub-agent thread we do not stream, and a stray `write_todos` turns a batch of
# three calls into a batch of four, which is a test failure about planning
# habits rather than about anything under test.
SYSTEM_PROMPT = (
    "You are an execution assistant. Do what the user asks, no more and no "
    "less. When asked to do several things at once, send every tool call "
    "together in one reply rather than one at a time. Never use the `task` "
    "tool and never use the `write_todos` tool: do the work yourself, directly."
)

CUSTOM_TOOLS = [
    {
        "type": "custom",
        "name": "get_crm_record",
        "description": "Look up one customer record in the CRM by its customer id.",
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
    {
        "type": "custom",
        "name": "ask_user",
        "description": "Ask the user a question and wait for their answer.",
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
]

AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "agent_toolset_20260401",
        "default_config": {"permission_policy": {"type": "always_allow"}},
        "configs": [
            {"name": "execute", "permission_policy": {"type": "always_ask"}},
            # Not because it is dangerous: `task` is the sub-agent entry point
            # and that work happens in a thread we do not stream. Making it ask
            # turns "the model quietly delegated" into a visible stop.
            {"name": "task", "permission_policy": {"type": "always_ask"}},
        ],
    },
    {"type": "web_toolset_20260401"},
    *CUSTOM_TOOLS,
]


# --- credentials -------------------------------------------------------------


@pytest.fixture(scope="session")
def settings():
    """Skip the whole suite, by name, rather than fail inside a provisioning
    call three fixtures later."""
    from app.config import get_settings

    resolved = get_settings()
    missing = [
        name
        for name in (
            "database_url",
            "openrouter_management_key",
            "e2b_api_key",
            "s3_endpoint_url",
            "s3_access_key_id",
            "s3_secret_access_key",
            "s3_bucket_name",
        )
        if not str(getattr(resolved, name, "")).strip()
    ]
    if missing:
        pytest.skip("live tests need: " + ", ".join(n.upper() for n in missing))
    if not str(resolved.database_url).startswith(("postgresql", "postgres://")):
        pytest.skip(f"live tests need Postgres, DATABASE_URL is {resolved.database_url!r}")
    return resolved


@pytest.fixture(scope="session", autouse=True)
def pinned_search(settings):
    """Replace `web_search` where the engine reads it.

    The engine imported the builder by name, so patching `app.runtime.tools`
    would leave the engine's own reference untouched.
    """
    import app.runtime.engine as engine
    from tests_live.stubs import pinned_web_search

    original = engine.web_search_tool
    engine.web_search_tool = pinned_web_search
    yield
    engine.web_search_tool = original


# --- the server --------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def server(settings, pinned_search) -> AsyncIterator[str]:
    from app.main import app as asgi_app

    port = _free_port()
    config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port, log_level="warning")
    running = uvicorn.Server(config)
    serving = asyncio.create_task(running.serve())
    while not running.started:
        if serving.done():
            await serving
        await asyncio.sleep(0.05)

    yield f"http://127.0.0.1:{port}"

    running.should_exit = True
    await serving


# --- the tenant and its catalogue --------------------------------------------


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def organization(server) -> str:
    """Written straight to the database: `/v1/organizations` is still stubs.

    A fresh slug per run keeps one run's rows from being mistaken for another's
    when something has to be looked at by hand afterwards.
    """
    from app.db.engine import session_scope
    from app.db.queries import organizations

    slug = f"live-{uuid.uuid4().hex[:10]}"
    async with session_scope() as db:
        created = await organizations.create_organization(db, slug=slug, name="Live tests")
        await db.commit()
        return created.id


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def api(server, organization) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=server,
        headers={"x-organization-id": organization},
        timeout=httpx.Timeout(TURN_TIMEOUT_SECONDS, connect=10.0),
    ) as client:
        yield client


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def environment(api) -> AsyncIterator[str]:
    """No packages, so it runs on the base image and there is nothing to build.

    The scenarios need a shell, a filesystem and Python's standard library.
    Declaring a dependency would add a minute of image build to every run to
    buy nothing.
    """
    response = await api.post(
        "/v1/environments",
        json={"name": f"live-{uuid.uuid4().hex[:8]}", "config": {}},
    )
    response.raise_for_status()
    created = response.json()
    assert created["build_state"] == "ready", created
    yield created["id"]
    # Archived, not deleted. Deleting a session only sets `deleted_at`, so the
    # rows keep pointing here and the foreign key refuses — which is the right
    # behaviour and not something a test should work around.
    await api.post(f"/v1/environments/{created['id']}/archive")


def _skill_zip() -> bytes:
    """Pack `fixtures/skill/` the way a client would."""
    root = FIXTURES / "skill"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                package.write(path, path.relative_to(root).as_posix())
    return buffer.getvalue()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def skill(api) -> AsyncIterator[str]:
    response = await api.post(
        "/v1/skills",
        files={"file": ("revenue-report.zip", _skill_zip(), "application/zip")},
    )
    response.raise_for_status()
    created = response.json()
    assert created["name"] == "revenue-report", created
    yield created["id"]
    await api.delete(f"/v1/skills/{created['id']}")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def uploads(api) -> AsyncIterator[dict[str, str]]:
    """The four files, uploaded once and mounted into every session."""
    ids: dict[str, str] = {}
    for name in UPLOAD_NAMES:
        response = await api.post(
            "/v1/files",
            files={"file": (name, (FIXTURES / "uploads" / name).read_bytes(), "text/plain")},
        )
        response.raise_for_status()
        ids[name] = response.json()["id"]
    yield ids
    # Not deleted, and not an oversight. A file that has been attached to a
    # session cannot be removed: `session_files` still references it, sessions
    # are only ever soft-deleted, so the foreign key refuses forever. Worse,
    # `delete_file` removes the bytes from the bucket *before* the row, so
    # calling it here would destroy the object, fail on the constraint, and
    # leave a file record pointing at nothing. Leaving four small files behind
    # is the lesser problem.


# The same assistant with the two prohibitions lifted. `task` and `write_todos`
# are the only tools the strict prompt rules out, and ruling them out is what
# makes a three-call batch reliably three calls — but it also left both of them
# with no coverage at all, and `task` is the *other* always_ask tool, so the
# whole delegation path went untested. Two agents rather than one compromise:
# the batch scenarios keep their quiet model, and delegation gets a real one.
PLANNING_PROMPT = (
    "You are an execution assistant. Do what the user asks, no more and no "
    "less. When asked to do several things at once, send every tool call "
    "together in one reply rather than one at a time. Use the planning and "
    "delegation tools when the user asks you to."
)


async def _create_agent(api, skill: str, system: str) -> str:
    response = await api.post(
        "/v1/agents",
        json={
            "name": f"live-{uuid.uuid4().hex[:8]}",
            "model": "claude-opus-5",
            "system": system,
            "tools": AGENT_TOOLS,
            "skills": [{"skill_id": skill}],
        },
    )
    response.raise_for_status()
    return response.json()["id"]


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def agent(api, skill) -> str:
    return await _create_agent(api, skill, SYSTEM_PROMPT)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def planning_agent(api, skill) -> str:
    return await _create_agent(api, skill, PLANNING_PROMPT)


# --- one session per test ----------------------------------------------------


async def _kill_sandbox(session_id: str) -> None:
    """Deleting a session soft-deletes the row; the container is separate.

    Left alone it would sit idle until E2B's own timeout, which is fifteen
    minutes of a paid container per test.
    """
    from app.db.engine import session_scope
    from app.db.queries import sessions as sessions_q
    from app.utils.sandbox import Sandbox

    async with session_scope() as db:
        row = await sessions_q.get_session_by_id(db, session_id=session_id)
        if row is None:
            return
        sandbox = await sessions_q.get_sandbox(
            db, session_id=session_id, organization_id=row.organization_id
        )
        if sandbox is None or not sandbox.external_sandbox_id:
            return
        container = Sandbox.from_id(
            sandbox.external_sandbox_id, session_id, row.organization_id
        )
    try:
        await container.kill()
    except Exception:
        # Already gone, or E2B is unreachable. Neither is worth failing a test
        # that has already made its point.
        pass


@pytest_asyncio.fixture(loop_scope="session")
async def new_session(api, agent, environment, uploads) -> AsyncIterator[Any]:
    """A factory, so a test that wants two sessions can have two.

    Every session it hands out is torn down at the end of the test, container
    included.
    """
    made: list[str] = []

    async def _create(**overrides: Any) -> str:
        body: dict[str, Any] = {
            "agent_id": agent,
            "environment_id": environment,
            "resources": [{"file_id": uploads[name], "path": name} for name in UPLOAD_NAMES],
        }
        body.update(overrides)
        response = await api.post("/v1/sessions", json=body)
        response.raise_for_status()
        session_id = response.json()["id"]
        made.append(session_id)
        return session_id

    yield _create

    for session_id in made:
        await _kill_sandbox(session_id)
        await api.delete(f"/v1/sessions/{session_id}")


@pytest_asyncio.fixture(loop_scope="session")
async def session(new_session) -> str:
    """One session with the four files mounted — what most scenarios want."""
    return await new_session()
