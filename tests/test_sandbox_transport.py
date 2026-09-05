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


# --- bounded byte reads -----------------------------------------------------


def _byte_read_sandbox(*, probe: str, content: bytes = b"abc"):
    sandbox = Sandbox.from_id("sbx_read", "ses_read", "org_read")
    sandbox.run = AsyncMock(return_value=SimpleNamespace(stdout=probe, exit_code=0))
    sandbox.ensure_connected = AsyncMock()
    read = AsyncMock(return_value=bytearray(content))
    sandbox._native = SimpleNamespace(files=SimpleNamespace(read=read))
    return sandbox, read


async def test_read_bytes_accepts_a_resolved_regular_file():
    sandbox, read = _byte_read_sandbox(probe="81a4 3")

    content = await sandbox.read_bytes("/home/user/chart.png", max_bytes=3)

    assert content == b"abc"
    command = sandbox.run.await_args.args[0]
    assert "stat -Lc '%f %s' -- /home/user/chart.png" in command
    sandbox.run.assert_awaited_once_with(command, idempotent=True)
    sandbox.ensure_connected.assert_awaited_once_with()
    read.assert_awaited_once_with(
        "/home/user/chart.png", format="bytes", user=sandbox_utils.GUEST_USER
    )


@pytest.mark.parametrize(
    "mode_hex",
    ["41ed", "11a4", "21b6"],
    ids=["directory", "fifo", "character-device"],
)
async def test_read_bytes_rejects_non_regular_targets_before_transfer(mode_hex):
    sandbox, read = _byte_read_sandbox(probe=f"{mode_hex} 0")

    with pytest.raises(ValueError, match="not a regular file"):
        await sandbox.read_bytes("/home/user/not-a-file.png", max_bytes=10)

    sandbox.ensure_connected.assert_not_awaited()
    read.assert_not_awaited()


async def test_read_bytes_rejects_an_oversized_regular_file_before_transfer():
    sandbox, read = _byte_read_sandbox(probe="81a4 11")

    with pytest.raises(ValueError, match="is 11 bytes, over the 10 byte limit"):
        await sandbox.read_bytes("/home/user/large.png", max_bytes=10)

    sandbox.ensure_connected.assert_not_awaited()
    read.assert_not_awaited()


async def test_read_bytes_preserves_the_missing_file_contract():
    sandbox, read = _byte_read_sandbox(probe="-1")

    with pytest.raises(FileNotFoundError):
        await sandbox.read_bytes("/home/user/missing.png", max_bytes=10)

    sandbox.ensure_connected.assert_not_awaited()
    read.assert_not_awaited()


async def test_read_bytes_rechecks_size_after_transfer():
    sandbox, read = _byte_read_sandbox(probe="81a4 3", content=b"abcd")

    with pytest.raises(ValueError, match="is 4 bytes, over the 3 byte limit"):
        await sandbox.read_bytes("/home/user/changing.png", max_bytes=3)

    read.assert_awaited_once()


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
