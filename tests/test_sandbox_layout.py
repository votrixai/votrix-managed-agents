from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.utils.sandbox import (
    COMPAT_OUTPUTS_DIR,
    COMPAT_SKILLS_DIR,
    COMPAT_UPLOADS_DIR,
    COMPAT_WORKDIR,
    OUTPUTS_DIR,
    SKILLS_DIR,
    UPLOADS_DIR,
    WORKDIR,
    Sandbox,
)


async def test_prepare_directories_adds_shared_agent_contract_aliases():
    sandbox = Sandbox.from_id("sbx_layout", "ses_layout", "org_layout")
    sandbox.run = AsyncMock(return_value=SimpleNamespace(exit_code=0))

    await sandbox.prepare_directories()

    sandbox.run.assert_awaited_once()
    command = sandbox.run.await_args.args[0]
    assert sandbox.run.await_args.kwargs == {"user": "root"}
    assert "mkdir -p /mnt/session" in command
    assert f"ln -sfnT {WORKDIR} {COMPAT_WORKDIR}" in command
    assert f"ln -sfnT {SKILLS_DIR} {COMPAT_SKILLS_DIR}" in command
    assert f"ln -sfnT {UPLOADS_DIR} {COMPAT_UPLOADS_DIR}" in command
    assert f"ln -sfnT {OUTPUTS_DIR} {COMPAT_OUTPUTS_DIR}" in command


async def test_prepare_directories_fails_closed_when_alias_creation_fails():
    sandbox = Sandbox.from_id("sbx_layout", "ses_layout", "org_layout")
    sandbox.run = AsyncMock(return_value=SimpleNamespace(exit_code=1))

    with pytest.raises(RuntimeError, match="compatibility layout"):
        await sandbox.prepare_directories()
