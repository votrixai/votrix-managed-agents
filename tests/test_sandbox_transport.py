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


# --- what "no digest" is allowed to mean ----------------------------------


def _sandbox(stdout: str, *, exit_code: int = 0, stderr: str = ""):
    native = _native(
        SimpleNamespace(stdout=stdout, exit_code=exit_code, stderr=stderr)
    )
    return native, Sandbox.from_id("sbx_d", "ses_d", "org_d")


async def _digest_of(monkeypatch, stdout, *, exit_code=0, stderr=""):
    native, sandbox = _sandbox(stdout, exit_code=exit_code, stderr=stderr)
    monkeypatch.setattr(
        sandbox_utils.AsyncSandbox, "connect", AsyncMock(return_value=native)
    )
    return await sandbox._digest("/home/user/outputs/project.video.zip")


async def test_a_missing_file_is_the_only_thing_that_reads_as_missing(monkeypatch):
    digest = await _digest_of(monkeypatch, f"{sandbox_utils._DIGEST_MISSING}\n")
    assert digest is None


async def test_a_present_file_gives_its_digest(monkeypatch):
    digest = await _digest_of(monkeypatch, f"{'b' * 64}  project.video.zip\n")
    assert digest == "b" * 64


async def test_a_broken_hasher_is_not_a_missing_file(monkeypatch):
    """The bug this exists to stop.

    `sha256sum` absent from the image, the command killed, the wrong container
    — all of it used to arrive as an empty string, become None, and be reported
    as a file that is not there. The agent could see the file in `ls`, so the
    only conclusion left to it was that the tool was broken.
    """

    # `2>&1` in the command means the complaint arrives on stdout, and the
    # marker means the shell still exits 0 — a non-zero exit would leave `run`
    # as an E2B exception and take the message with it.
    with pytest.raises(RuntimeError) as caught:
        await _digest_of(
            monkeypatch,
            f"sha256sum: command not found\n{sandbox_utils._DIGEST_FAILED}\n",
        )

    assert "command not found" in str(caught.value)
    assert sandbox_utils._DIGEST_FAILED not in str(caught.value)


async def test_output_that_is_not_a_digest_is_not_trusted(monkeypatch):
    """Whatever this is, it is not sixty-four hex characters."""

    with pytest.raises(RuntimeError):
        await _digest_of(monkeypatch, f"Killed\n{sandbox_utils._DIGEST_FAILED}\n")


async def test_the_providers_429_is_not_left_as_an_sdk_error(monkeypatch):
    """E2B's own concurrency limit, said in words a caller can act on."""
    from e2b.exceptions import RateLimitException
    from app.models.errors import ProviderRateLimited

    async def _refuse(**kwargs):
        raise RateLimitException(
            "429: Rate limit exceeded — you have reached the maximum number of "
            "concurrent E2B sandboxes (20)."
        )

    monkeypatch.setattr(sandbox_utils.AsyncSandbox, "create", _refuse)

    with pytest.raises(ProviderRateLimited) as caught:
        await sandbox_utils._start_container(template="base")

    assert "concurrent E2B sandboxes (20)" in str(caught.value)
