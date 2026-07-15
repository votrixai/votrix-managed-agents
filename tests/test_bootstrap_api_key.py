from sqlalchemy import func, select

from app.db.engine import session_scope
from app.db.models import ApiKey, Workspace
from app.db.queries import api_keys as api_keys_q
from scripts.bootstrap_api_key import BootstrapConflict, bootstrap_api_key


async def test_bootstrap_api_key_creates_workspace_and_one_hashed_admin_key():
    result = await bootstrap_api_key(
        workspace_id="wrkspc_bootstrap",
        workspace_slug="bootstrap",
        workspace_name="Bootstrap tenant",
    )

    assert result.workspace_created is True
    assert result.secret.startswith("vma_")
    assert result.prefix == result.secret[:12]

    async with session_scope() as db:
        workspace = await db.get(Workspace, "wrkspc_bootstrap")
        stored = await api_keys_q.get_api_key(
            db,
            result.key_id,
            workspace_id="wrkspc_bootstrap",
        )

    assert workspace is not None
    assert workspace.slug == "bootstrap"
    assert stored is not None
    assert stored.scopes == [api_keys_q.API_SCOPE, api_keys_q.API_KEYS_MANAGE_SCOPE]
    assert stored.created_by == "bootstrap_api_key"
    assert stored.key_hash == api_keys_q.hash_api_key(result.secret)
    assert stored.key_hash != result.secret


async def test_bootstrap_refuses_duplicate_active_admin_without_explicit_override():
    await bootstrap_api_key(workspace_id="wrkspc_bootstrap_guard")

    try:
        await bootstrap_api_key(workspace_id="wrkspc_bootstrap_guard")
    except BootstrapConflict as exc:
        assert "already has an active management key" in str(exc)
    else:
        raise AssertionError("duplicate bootstrap unexpectedly succeeded")

    additional = await bootstrap_api_key(
        workspace_id="wrkspc_bootstrap_guard",
        allow_additional_admin_key=True,
    )
    assert additional.workspace_created is False

    async with session_scope() as db:
        count = await db.scalar(
            select(func.count()).select_from(ApiKey).where(
                ApiKey.workspace_id == "wrkspc_bootstrap_guard"
            )
        )
    assert count == 2
