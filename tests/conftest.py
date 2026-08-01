"""Shared fixtures.

Everything runs against an in-memory SQLite database built fresh per test.
Nothing here is allowed to reach the configured Postgres, E2B, or any model
provider — a test that needs one of those stubs it explicitly.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base
from app.db.queries import accounts, agents, environments
from app.db.queries import sessions as sessions_q


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def org(db):
    organization = await accounts.create_organization(db, slug="acme", name="Acme")
    await db.commit()
    return organization.id


@pytest_asyncio.fixture
async def agent(db, org):
    created, _ = await agents.create_agent(
        db,
        organization_id=org,
        name="bot",
        model={"id": "claude-opus-5"},
        tools=[{"type": "agent_toolset_20260401"}],
    )
    await db.commit()
    return created


@pytest_asyncio.fixture
async def environment(db, org):
    from app.utils.sandbox import Image

    created = await environments.create_environment(
        db,
        organization_id=org,
        name="default",
        config={},
        image_id=Image.base().image_id,
    )
    await db.commit()
    return created


@pytest_asyncio.fixture
async def session(db, org, agent, environment):
    """A session that already exists, created without touching E2B."""
    created = await sessions_q.create_session(
        db,
        organization_id=org,
        agent_id=agent.id,
        agent_version=agent.active_version,
        environment_id=environment.id,
        title="test",
    )
    await db.commit()
    return created


@pytest.fixture(autouse=True)
def never_dispatch(monkeypatch):
    """Stop `send_events` from actually running a turn.

    Accepting a message and running it are separate concerns; these tests are
    about the first. Recorded calls are available as `.calls` for the tests
    that care that dispatch happened at all.
    """
    from app.services import sessions as service

    calls = []

    async def _record(db, *, session_id, generation, events):
        calls.append({"session_id": session_id, "generation": generation, "events": events})

    monkeypatch.setattr(service, "_dispatch_turn", _record)
    _record.calls = calls
    return _record


@pytest.fixture
def cloud_dispatch(monkeypatch):
    """Put the app into `cloud` dispatch. Yields the service account that the
    queue is supposed to authenticate as."""
    from app.config import clear_settings_cache

    account = "runner@votrix-prod.iam.gserviceaccount.com"
    for name, value in {
        "TURN_DISPATCH": "cloud",
        "TASKS_PROJECT": "votrix-prod",
        "TASKS_LOCATION": "us-central1",
        "TASKS_QUEUE": "turns",
        "TASKS_SERVICE_ACCOUNT": account,
        "WORKER_URL": "https://api.votrix.example",
    }.items():
        monkeypatch.setenv(name, value)
    clear_settings_cache()

    yield account

    clear_settings_cache()


@pytest.fixture
def builds(monkeypatch):
    """Stand in for the provider's image builder.

    `finish()` decides what the next status check reports, so a test can end a
    build without waiting for one.
    """
    from app.db.models.environments import BUILDING
    from app.db.queries import environments as environments_q
    from app.utils.sandbox import BuildStatus, Image

    class Builder:
        def __init__(self) -> None:
            self.started = []
            self.state = "building"
            self.error = None

        def finish(self, state: str = "ready", error: str | None = None) -> None:
            self.state = state
            self.error = error

    builder = Builder()

    async def _build(cls, db, environment, *, packages, cpu, memory_mb):
        builder.started.append(
            {"name": environment.id, "packages": packages, "cpu": cpu, "memory_mb": memory_mb}
        )
        image = Image(
            f"img_{len(builder.started)}",
            f"bld_{len(builder.started)}",
            environment.id,
        )
        await environments_q.set_build(
            db, environment, state=BUILDING, image_id=image.image_id, build_id=image.build_id
        )
        return image

    async def _status(self):
        return BuildStatus(state=builder.state, error=builder.error)

    monkeypatch.setattr(Image, "build", classmethod(_build))
    monkeypatch.setattr(Image, "status", _status)
    return builder


@pytest.fixture
def sandboxes(monkeypatch):
    """Stand in for E2B while keeping the database side real.

    The row still gets written, because everything downstream of provisioning
    reads it. What a test inspects is `.calls` — the image, skills and files
    the container was asked to start with.
    """
    from app.db.queries import sessions as sessions_q
    from app.utils.sandbox import Sandbox

    calls = []

    async def _provision(
        cls,
        db,
        session,
        *,
        image,
        skill_ids,
        files,
        memory_mounts,
    ):
        calls.append(
            {
                "image": image.image_id,
                "skill_ids": skill_ids,
                "files": files,
                "memory_mounts": memory_mounts,
            }
        )
        row = await sessions_q.create_sandbox(
            db, session, provider="e2b", external_sandbox_id="sbx_fake"
        )
        await sessions_q.update_sandbox_state(db, row, state="running")
        return cls("sbx_fake", session.id, session.organization_id)

    monkeypatch.setattr(Sandbox, "provision", classmethod(_provision))
    return calls


@pytest.fixture
def volumes(monkeypatch):
    """Stand in for E2B Volume lifecycle calls while keeping DB state real."""
    from app.utils.volume import Volume

    class Provider:
        def __init__(self) -> None:
            self.created = []
            self.destroyed = []
            self.files = {}
            self.create_error = None
            self.destroy_error = None
            self.write_error = None
            self.remove_error = None

    provider = Provider()

    async def _provision(cls, store):
        if provider.create_error is not None:
            raise provider.create_error
        locator = {
            "volume_id": f"vol_{len(provider.created) + 1}",
            "volume_name": cls.provider_name(store.id),
        }
        provider.created.append({"memory_store_id": store.id, **locator})
        provider.files.setdefault(store.id, {})
        return locator

    async def _destroy(cls, store):
        if provider.destroy_error is not None:
            raise provider.destroy_error
        provider.destroyed.append(
            {
                "memory_store_id": store.id,
                "volume_locator": dict(store.volume_locator or {}),
            }
        )
        provider.files.pop(store.id, None)

    async def _write_file(cls, store, path, content):
        if provider.write_error is not None:
            raise provider.write_error
        provider.files.setdefault(store.id, {})[path] = content

    async def _remove_file(cls, store, path):
        if provider.remove_error is not None:
            raise provider.remove_error
        provider.files.setdefault(store.id, {}).pop(path, None)

    monkeypatch.setattr(Volume, "provision", classmethod(_provision))
    monkeypatch.setattr(Volume, "destroy", classmethod(_destroy))
    monkeypatch.setattr(Volume, "write_file", classmethod(_write_file))
    monkeypatch.setattr(Volume, "remove_file", classmethod(_remove_file))
    return provider
