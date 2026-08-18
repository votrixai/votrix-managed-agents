from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpcore import ReadError
from httpx import WriteError

from app.utils import sandbox as sandbox_utils
from app.utils.sandbox import Sandbox


def _native(*outcomes):
    return SimpleNamespace(
        commands=SimpleNamespace(run=AsyncMock(side_effect=list(outcomes)))
    )


async def test_connect_retries_a_dropped_e2b_control_plane_write(monkeypatch):
    native = _native()
    connect = AsyncMock(side_effect=[WriteError("connection reset"), native])
    monkeypatch.setattr(sandbox_utils.AsyncSandbox, "connect", connect)
    monkeypatch.setattr(sandbox_utils, "E2B_TRANSPORT_RETRY_DELAY_SECONDS", 0)
    sandbox = Sandbox.from_id("sbx_retry", "ses_retry", "org_retry")

    await sandbox.ensure_connected()

    assert connect.await_count == 2
    assert sandbox._native is native


async def test_digest_reconnects_after_a_dropped_e2b_command_read(monkeypatch):
    first = _native(ReadError("connection reset"))
    second = _native(SimpleNamespace(stdout=f"{'a' * 64}  report.pdf\n", exit_code=0))
    connect = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(sandbox_utils.AsyncSandbox, "connect", connect)
    monkeypatch.setattr(sandbox_utils, "E2B_TRANSPORT_RETRY_DELAY_SECONDS", 0)
    sandbox = Sandbox.from_id("sbx_retry", "ses_retry", "org_retry")

    digest = await sandbox._digest("/home/user/outputs/report.pdf")

    assert digest == "a" * 64
    assert connect.await_count == 2
    first.commands.run.assert_awaited_once()
    second.commands.run.assert_awaited_once()


async def test_non_idempotent_command_does_not_retry_an_ambiguous_read(monkeypatch):
    native = _native(ReadError("connection reset"))
    connect = AsyncMock(return_value=native)
    monkeypatch.setattr(sandbox_utils.AsyncSandbox, "connect", connect)
    monkeypatch.setattr(sandbox_utils, "E2B_TRANSPORT_RETRY_DELAY_SECONDS", 0)
    sandbox = Sandbox.from_id("sbx_no_retry", "ses_no_retry", "org_no_retry")

    with pytest.raises(ReadError):
        await sandbox.run("do-something-with-side-effects")

    connect.assert_awaited_once()
    native.commands.run.assert_awaited_once()
