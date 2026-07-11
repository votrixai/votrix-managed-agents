from datetime import datetime, timedelta, timezone

import pytest

from app.db.engine import session_scope
from app.db.queries import agents as agents_q
from app.db.queries import environments as environments_q
from app.db.queries import session_sandboxes as sandboxes_q
from app.db.queries import sessions as sessions_q


async def _create_session(*, workspace_id: str, suffix: str = "default") -> str:
    async with session_scope() as db:
        agent, version = await agents_q.create_agent(
            db,
            name=f"Sandbox agent {workspace_id} {suffix}",
            model={"id": "claude-sonnet-4-6"},
            workspace_id=workspace_id,
        )
        environment = await environments_q.create_environment(
            db,
            name=f"sandbox-{workspace_id}-{suffix}",
            config={"type": "cloud"},
            workspace_id=workspace_id,
        )
        session = await sessions_q.create_session(
            db,
            agent=agent,
            agent_version=version.version,
            environment=environment,
            workspace_id=workspace_id,
        )
        await db.commit()
        return session.id


async def test_session_sandbox_upsert_keeps_exactly_one_row_per_session():
    workspace_id = "wrkspc_sandbox_a"
    session_id = await _create_session(workspace_id=workspace_id)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    async with session_scope() as db:
        created = await sandboxes_q.upsert_session_sandbox(
            db,
            workspace_id=workspace_id,
            session_id=session_id,
            provider="e2b",
            state="provisioning",
            template_id="vma-python",
            config={"bootstrap_digest": "sha256:initial"},
            capabilities={"execute": True, "filesystem": True},
            expires_at=expires_at,
        )
        created_id = created.id
        assert created.lock_version == 0

        updated = await sandboxes_q.upsert_session_sandbox(
            db,
            workspace_id=workspace_id,
            session_id=session_id,
            provider="e2b",
            external_sandbox_id="e2b_session_123",
            state="paused",
            template_id="vma-python",
            config={"bootstrap_digest": "sha256:initial"},
            capabilities={"execute": True, "filesystem": True},
            expires_at=expires_at,
        )

        assert updated.id == created_id
        assert updated.external_sandbox_id == "e2b_session_123"
        assert updated.state == "paused"
        assert updated.lock_version == 1

        locked = await sandboxes_q.get_session_sandbox(
            db,
            session_id,
            workspace_id=workspace_id,
            for_update=True,
        )
        by_external_id = await sandboxes_q.get_session_sandbox_by_external_id(
            db,
            provider="e2b",
            external_sandbox_id="e2b_session_123",
            workspace_id=workspace_id,
            for_update=True,
        )
        await db.commit()

    assert locked is not None and locked.id == created_id
    assert by_external_id is not None and by_external_id.id == created_id


async def test_session_sandbox_queries_and_upserts_are_workspace_safe():
    owner_workspace_id = "wrkspc_sandbox_owner"
    other_workspace_id = "wrkspc_sandbox_other"
    session_id = await _create_session(workspace_id=owner_workspace_id)

    async with session_scope() as db:
        sandbox = await sandboxes_q.upsert_session_sandbox(
            db,
            workspace_id=owner_workspace_id,
            session_id=session_id,
            provider="e2b",
            external_sandbox_id="e2b_tenant_123",
        )
        assert sandbox.workspace_id == owner_workspace_id
        assert await sandboxes_q.get_session_sandbox(
            db,
            session_id,
            workspace_id=other_workspace_id,
        ) is None
        assert await sandboxes_q.get_session_sandbox_by_external_id(
            db,
            provider="e2b",
            external_sandbox_id="e2b_tenant_123",
            workspace_id=other_workspace_id,
        ) is None

        with pytest.raises(sandboxes_q.SessionSandboxSessionNotFoundError):
            await sandboxes_q.upsert_session_sandbox(
                db,
                workspace_id=other_workspace_id,
                session_id=session_id,
                provider="e2b",
            )


async def test_session_sandbox_state_update_uses_optimistic_lock_version():
    workspace_id = "wrkspc_sandbox_state"
    session_id = await _create_session(workspace_id=workspace_id)
    now = datetime.now(timezone.utc)

    async with session_scope() as db:
        sandbox = await sandboxes_q.upsert_session_sandbox(
            db,
            workspace_id=workspace_id,
            session_id=session_id,
            provider="e2b",
            state="provisioning",
        )
        original_state_changed_at = sandbox.state_changed_at

        transitioned = await sandboxes_q.update_session_sandbox_state(
            db,
            sandbox,
            workspace_id=workspace_id,
            state="paused",
            expected_lock_version=0,
            external_sandbox_id="e2b_state_123",
            last_active_at=now,
            expires_at=now + timedelta(minutes=30),
            error=None,
        )

        assert transitioned.state == "paused"
        assert transitioned.external_sandbox_id == "e2b_state_123"
        assert transitioned.last_active_at is not None
        assert transitioned.expires_at is not None
        assert transitioned.state_changed_at >= original_state_changed_at
        assert transitioned.lock_version == 1

        with pytest.raises(sandboxes_q.SessionSandboxLockConflictError):
            await sandboxes_q.update_session_sandbox_state(
                db,
                sandbox,
                workspace_id=workspace_id,
                state="deleted",
                expected_lock_version=0,
            )

        refreshed = await sandboxes_q.get_session_sandbox(
            db,
            session_id,
            workspace_id=workspace_id,
        )

    assert refreshed is not None
    assert refreshed.state == "paused"
    assert refreshed.lock_version == 1


async def test_expired_cleanup_only_returns_inactive_provider_sandboxes():
    workspace_id = "wrkspc_sandbox_cleanup"
    expired_session_id = await _create_session(
        workspace_id=workspace_id,
        suffix="expired",
    )
    running_session_id = await _create_session(
        workspace_id=workspace_id,
        suffix="running",
    )
    future_session_id = await _create_session(
        workspace_id=workspace_id,
        suffix="future",
    )
    now = datetime.now(timezone.utc)

    async with session_scope() as db:
        expired = await sandboxes_q.upsert_session_sandbox(
            db,
            workspace_id=workspace_id,
            session_id=expired_session_id,
            provider="e2b",
            external_sandbox_id="e2b_expired",
            state="paused",
            expires_at=now - timedelta(seconds=1),
        )
        await sandboxes_q.upsert_session_sandbox(
            db,
            workspace_id=workspace_id,
            session_id=running_session_id,
            provider="e2b",
            external_sandbox_id="e2b_running",
            state="running",
            expires_at=now - timedelta(seconds=1),
        )
        await sandboxes_q.upsert_session_sandbox(
            db,
            workspace_id=workspace_id,
            session_id=future_session_id,
            provider="e2b",
            external_sandbox_id="e2b_future",
            state="paused",
            expires_at=now + timedelta(hours=1),
        )

        candidates = await sandboxes_q.list_expired_session_sandboxes_for_cleanup(
            db,
            now=now,
            provider="e2b",
        )

    assert [candidate.id for candidate in candidates] == [expired.id]
