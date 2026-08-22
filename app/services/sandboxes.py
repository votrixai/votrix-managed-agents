"""Containers handed straight to a caller, with no agent in them.

A Session's container is filled with skills and uploads and driven by a model.
This is the same container with none of that: a caller names an environment,
gets an id back, runs commands against it, and deletes it. What it is for is
the work that needs a machine rather than a conversation — checking a file
against rules that only exist as a program, converting something, running a
tool we do not want installed in this service's own image.

Two things here are load-bearing and neither is obvious.

**Output is captured inside the container, never streamed through here.** A
command's stdout is redirected to a file on the container's own disk and only
a bounded head of it is ever read back. Capping the response instead would be
no protection at all: by the time this process could truncate anything, it
would already be holding whatever the command printed.

**A command's state lives in the container's filesystem, not in memory.** The
launcher writes `rc` when the command finishes, so "is it done" is a question
about a file. A handle from `commands.run(background=True)` would not survive
the end of the request that created it, and the next poll is very likely to
land on a different instance.
"""

from __future__ import annotations

import base64
import shlex

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Environment, File, Sandbox
from app.db.models.sandboxes import (
    LIVE_SANDBOX_STATES,
    SANDBOX_FAILED,
    SANDBOX_RUNNING,
    SANDBOX_TERMINATED,
)
from app.db.queries import DEFAULT_PAGE_SIZE, Page
from app.db.queries import environments as environments_q
from app.db.queries import sandboxes as sandboxes_q
from app.models.errors import Conflict, InvalidRequest, NotFound
from app.models.sandbox import (
    EXEC_FAILED,
    EXEC_RUNNING,
    EXEC_SUCCEEDED,
    EXEC_TIMED_OUT,
    MAX_OUTPUT_CHARS,
)
from app.services import environments as environments_service
from app.utils.id_generator import new_id
from app.utils.sandbox import WORKDIR, Image
from app.utils.sandbox import Sandbox as Container

logger = structlog.get_logger(__name__)

# Where an exec's working directory and its captured output live. One
# directory per command, so two commands in the same container never read each
# other's output and a caller can still fetch either afterwards.
EXECS_DIR = f"{WORKDIR}/execs"

# `timeout` reports a command it killed with this code. It is the shell's own
# convention, which is why the launcher does not have to say anything itself.
_TIMEOUT_EXIT_CODE = 124

# Slack on the transport call that runs the launcher or waits on it, over and
# above whatever the caller asked to wait. The wait happens inside the
# container; this only has to outlive it.
_TRANSPORT_SLACK_SECONDS = 30


class SandboxGone(Conflict):
    """The container is no longer there to work in."""


# --- lifecycle -------------------------------------------------------------


async def create_sandbox(
    db: AsyncSession,
    *,
    organization_id: str,
    environment_id: str,
    ttl_seconds: int,
    network_access: bool,
) -> Sandbox:
    """Start a container and hand back its id.

    A failed start is written down as a `failed` row rather than thrown away,
    so a caller that got a 5xx can still find out what happened to the
    container it may or may not have.
    """
    environment = await _environment(
        db, organization_id=organization_id, environment_id=environment_id
    )

    live = await sandboxes_q.count_live(db, organization_id=organization_id)
    cap = get_settings().max_sandboxes_per_organization
    if live >= cap:
        raise Conflict(
            f"This organization already holds {live} sandboxes, which is the "
            f"limit of {cap}. Delete one before creating another."
        )

    image = Image.from_environment(environment) or Image.base()
    row = await sandboxes_q.create_sandbox(
        db,
        organization_id=organization_id,
        environment_id=environment.id,
        ttl_seconds=ttl_seconds,
        network_access=network_access,
    )
    try:
        container = await Container.start(
            image=image,
            scope_id=row.id,
            organization_id=organization_id,
            ttl_seconds=ttl_seconds,
            network_access=network_access,
        )
    except Exception as exc:
        await sandboxes_q.set_state(
            db, row, state=SANDBOX_FAILED, error={"message": str(exc)[:500]}
        )
        await db.commit()
        logger.warning(
            "sandbox_start_failed",
            sandbox_id=row.id,
            environment_id=environment.id,
            error=type(exc).__name__,
        )
        raise

    row.external_sandbox_id = container.sandbox_id
    await db.commit()
    logger.info(
        "sandbox_created",
        sandbox_id=row.id,
        environment_id=environment.id,
        ttl_seconds=ttl_seconds,
        network_access=network_access,
    )
    return row


async def get_sandbox(
    db: AsyncSession,
    *,
    sandbox_id: str,
    organization_id: str,
) -> Sandbox:
    """Read one.

    A container past `expires_at` is not gone — the provider has paused it and
    the next call wakes it. So there is nothing to settle here: the row says
    what it said, and only an explicit kill changes it.
    """
    row = await sandboxes_q.get_sandbox(
        db, sandbox_id=sandbox_id, organization_id=organization_id
    )
    if row is None:
        raise NotFound(f"Sandbox {sandbox_id} not found")
    return row


async def list_sandboxes(
    db: AsyncSession,
    *,
    organization_id: str,
    state: str | None = None,
    include_session_owned: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    return await sandboxes_q.list_sandboxes(
        db,
        organization_id=organization_id,
        state=state,
        include_session_owned=include_session_owned,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )


async def delete_sandbox(
    db: AsyncSession,
    *,
    sandbox_id: str,
    organization_id: str,
) -> Sandbox:
    """Destroy the container. The only thing that ever does.

    A container is never collected on a timer — it pauses and waits — so this
    is how one ends, and refusing it for a session's container is not a
    technicality. Killing that container ends the conversation: the next turn
    finds nothing to run in and terminates the session. A call named "delete a
    sandbox" should not be able to do that, and the session API has a verb that
    says what it means.

    A container that has already gone is not an error: the caller asked for it
    to be gone and it is. Only the row's state has to end up right.
    """
    row = await get_sandbox(
        db, sandbox_id=sandbox_id, organization_id=organization_id
    )
    if row.is_session_owned:
        raise Conflict(
            f"Sandbox {sandbox_id} belongs to session {row.session_id}. Deleting "
            f"it would end that conversation — delete the session instead."
        )
    if row.state in LIVE_SANDBOX_STATES and row.external_sandbox_id:
        try:
            await _container(row).kill()
        except Exception as exc:
            logger.info(
                "sandbox_already_gone", sandbox_id=row.id, error=type(exc).__name__
            )
    await sandboxes_q.set_state(db, row, state=SANDBOX_TERMINATED)
    await db.commit()
    return row


# --- exec ------------------------------------------------------------------


async def exec_command(
    db: AsyncSession,
    *,
    sandbox_id: str,
    organization_id: str,
    command: str,
    cwd: str | None,
    timeout_seconds: int,
    wait_seconds: int,
) -> dict:
    """Start one command, wait a bounded time for it, and report where it got to.

    Setting up, launching, waiting and reporting are one call to the container.
    They were five, and the four extra were the single largest cost in a check:
    each one is a round trip to the provider, which was measured at between
    0.9 and 2.5 seconds — far more than anything the command itself did.

    The caller's command still never becomes part of a shell line. It travels
    base64-encoded, and base64 is drawn from an alphabet with no shell
    metacharacter in it, so the encoded form is safe to interpolate where the
    raw command would not be.
    """
    row = await _live(db, sandbox_id=sandbox_id, organization_id=organization_id)
    container = _container(row)

    exec_id = new_id("exec")
    directory = f"{EXECS_DIR}/{exec_id}"
    workdir = _resolve_cwd(cwd, directory)

    script = _start(directory, workdir, command, timeout_seconds) + _collector(
        directory, wait_seconds
    )
    result = await _run_report(
        container,
        script,
        directory,
        exec_id=exec_id,
        sandbox_id=row.id,
        wait_seconds=wait_seconds,
    )
    await sandboxes_q.touch(db, row)
    await db.commit()
    return result


async def exec_result(
    db: AsyncSession,
    *,
    sandbox_id: str,
    organization_id: str,
    exec_id: str,
) -> dict:
    """Read where a command got to, without waiting."""
    row = await _live(db, sandbox_id=sandbox_id, organization_id=organization_id)
    directory = f"{EXECS_DIR}/{exec_id}"

    result = await _run_report(
        _container(row),
        _collector(directory, 0),
        directory,
        exec_id=exec_id,
        sandbox_id=row.id,
        wait_seconds=0,
    )
    await sandboxes_q.touch(db, row)
    await db.commit()
    return result


# --- files -----------------------------------------------------------------


async def upload_file(
    db: AsyncSession,
    *,
    sandbox_id: str,
    organization_id: str,
    file_id: str,
    path: str,
) -> Sandbox:
    """Put a stored File into the container at `path`."""
    row = await _live(db, sandbox_id=sandbox_id, organization_id=organization_id)
    await _container(row).upload_file(db, _absolute(path), file_id)
    await sandboxes_q.touch(db, row)
    await db.commit()
    return row


async def download_file(
    db: AsyncSession,
    *,
    sandbox_id: str,
    organization_id: str,
    path: str,
    filename: str | None = None,
) -> File:
    """Take one file out of the container and give it a File row."""
    row = await _live(db, sandbox_id=sandbox_id, organization_id=organization_id)
    file = await _container(row).download_file(
        db, _absolute(path), scope_id=row.id, filename=filename
    )
    await sandboxes_q.touch(db, row)
    await db.commit()
    return file


# --- internals -------------------------------------------------------------


async def _environment(
    db: AsyncSession, *, organization_id: str, environment_id: str
) -> Environment:
    """The image to start.

    Named by the caller, always. This service builds images from a list of
    packages and has no opinion about what they hold — a registry of
    ready-made ones here would mean keeping other people's recipes, which is
    how a runtime turns into a cupboard.
    """
    found = await environments_q.get_environment(
        db, environment_id=environment_id, organization_id=organization_id
    )
    if found is None:
        raise NotFound(f"Environment {environment_id} not found")
    return await environments_service.require_usable(db, found)


def _container(row: Sandbox) -> Container:
    return Container.from_id(
        row.external_sandbox_id or "",
        row.id,
        row.organization_id,
        ttl_seconds=row.ttl_seconds,
    )


def _absolute(path: str) -> str:
    """Resolve a caller's path against the container's working directory."""
    cleaned = path.strip()
    if not cleaned:
        raise InvalidRequest("path is required")
    return cleaned if cleaned.startswith("/") else f"{WORKDIR}/{cleaned}"


def _resolve_cwd(cwd: str | None, directory: str) -> str:
    """Where the command runs. Its own exec directory unless told otherwise."""
    if cwd is None or not cwd.strip():
        return directory
    return _absolute(cwd)


async def _live(
    db: AsyncSession, *, sandbox_id: str, organization_id: str
) -> Sandbox:
    """The row, refused unless there is still a container behind it."""
    row = await get_sandbox(
        db, sandbox_id=sandbox_id, organization_id=organization_id
    )
    if row.state not in LIVE_SANDBOX_STATES:
        raise SandboxGone(f"Sandbox {sandbox_id} is {row.state}")
    if not row.external_sandbox_id:
        raise SandboxGone(f"Sandbox {sandbox_id} never started")
    return row


def _launcher(directory: str, workdir: str, timeout_seconds: int) -> str:
    """The script that runs the caller's command and records what happened.

    Everything the caller sends is in `cmd.sh`, which this only ever names.
    `rc` is written last and is what makes the command's state readable by a
    later request: until it exists the command is running, and once it exists
    everything else about the run is already on disk.
    """
    d = shlex.quote(directory)
    return f"""#!/bin/sh
date +%s > {d}/started
mkdir -p {shlex.quote(workdir)} 2>/dev/null
cd {shlex.quote(workdir)} || cd {d}
timeout {int(timeout_seconds)} sh {d}/cmd.sh > {d}/stdout 2> {d}/stderr
code=$?
date +%s > {d}/ended
echo $code > {d}/rc
"""


def _embed(content: str, path: str) -> str:
    """A shell line that writes `content` to `path`, byte for byte.

    The content is base64 on the way through. That is not for size — it is
    what makes this safe: the alphabet is `A-Za-z0-9+/=`, which holds no
    quote, no newline and no substitution, so the encoded form can be
    interpolated into a command where the raw text could not. A caller's shell
    command is arbitrary text and routinely contains all three.
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return f"printf %s '{encoded}' | base64 -d > {shlex.quote(path)}\n"


def _start(directory: str, workdir: str, command: str, timeout_seconds: int) -> str:
    """Everything up to the command running, as one script.

    This was five calls to the container — a `mkdir`, two writes that each did
    their own `mkdir` first, and the launch. Every one of them was a round trip
    to the provider, measured at one to two and a half seconds, which together
    cost more than the work being asked for.

    `setsid` detaches the launcher from the shell the provider closes when this
    returns, so the command outlives the call whatever the caller asked to
    wait for.
    """
    d = shlex.quote(directory)
    return (
        f"mkdir -p {d}\n"
        + _embed(command, f"{directory}/cmd.sh")
        + _embed(_launcher(directory, workdir, timeout_seconds), f"{directory}/run.sh")
        + f"setsid sh {d}/run.sh >/dev/null 2>&1 &\n"
    )


def _collector(directory: str, wait_seconds: int) -> str:
    """One command that waits for the run, then reports it in a parseable form.

    Waiting happens here, inside the container, rather than as a poll loop
    from this process: one round trip instead of one every couple of hundred
    milliseconds, and the sleeping is done by something that is not an event
    loop serving other requests.

    Output comes back base64-encoded. What a command prints is arbitrary
    bytes — it can contain newlines, or anything chosen to look like the rest
    of this report — and encoding it is what keeps the framing honest.
    """
    d = shlex.quote(directory)
    wait = (
        f"""deadline=$(( $(date +%s) + {int(wait_seconds)} ))
while [ ! -f {d}/rc ]; do
  [ "$(date +%s)" -ge "$deadline" ] && break
  sleep 0.2
done
"""
        if wait_seconds > 0
        else ""
    )
    return f"""{wait}[ -d {d} ] && echo "FOUND:1" || echo "FOUND:0"
echo "RC:$(cat {d}/rc 2>/dev/null)"
echo "STARTED:$(cat {d}/started 2>/dev/null)"
echo "ENDED:$(cat {d}/ended 2>/dev/null)"
echo "OUTLEN:$(wc -c < {d}/stdout 2>/dev/null || echo 0)"
echo "ERRLEN:$(wc -c < {d}/stderr 2>/dev/null || echo 0)"
echo "OUT:$(head -c {MAX_OUTPUT_CHARS} {d}/stdout 2>/dev/null | base64 | tr -d '\\n')"
echo "ERR:$(head -c {MAX_OUTPUT_CHARS} {d}/stderr 2>/dev/null | base64 | tr -d '\\n')"
"""


async def _run_report(
    container: Container,
    script: str,
    directory: str,
    *,
    exec_id: str,
    sandbox_id: str,
    wait_seconds: int,
) -> dict:
    """Run one script that ends in a report, and turn the report into a result."""

    result = await container.run(
        script, timeout=wait_seconds + _TRANSPORT_SLACK_SECONDS
    )
    report = _parse(result.stdout or "")

    if report.get("FOUND") == "0":
        raise NotFound(f"Exec {exec_id} not found in sandbox {sandbox_id}")

    rc = report.get("RC")
    exit_code = int(rc) if rc not in (None, "") else None
    out_len = int(report.get("OUTLEN") or 0)
    err_len = int(report.get("ERRLEN") or 0)

    if exit_code is None:
        state = EXEC_RUNNING
    elif exit_code == _TIMEOUT_EXIT_CODE:
        state = EXEC_TIMED_OUT
    elif exit_code == 0:
        state = EXEC_SUCCEEDED
    else:
        state = EXEC_FAILED

    started, ended = report.get("STARTED"), report.get("ENDED")
    duration_ms = (
        (int(ended) - int(started)) * 1000
        if started and ended and started.isdigit() and ended.isdigit()
        else None
    )

    return {
        "id": exec_id,
        "sandbox_id": sandbox_id,
        "state": state,
        "exit_code": exit_code,
        "stdout": _decode(report.get("OUT")),
        "stderr": _decode(report.get("ERR")),
        "stdout_truncated": out_len > MAX_OUTPUT_CHARS,
        "stderr_truncated": err_len > MAX_OUTPUT_CHARS,
        "dir": directory,
        "duration_ms": duration_ms,
    }


def _parse(stdout: str) -> dict[str, str]:
    report: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            report[key] = value
    return report


def _decode(encoded: str | None) -> str:
    if not encoded:
        return ""
    try:
        return base64.b64decode(encoded).decode("utf-8", "replace")
    except Exception:
        # A report we cannot decode is a bug here, not a failure of the
        # command. Saying nothing is better than handing back the encoding.
        logger.warning("exec_output_undecodable", length=len(encoded))
        return ""


__all__ = [
    "SandboxGone",
    "create_sandbox",
    "delete_sandbox",
    "download_file",
    "exec_command",
    "exec_result",
    "get_sandbox",
    "list_sandboxes",
    "upload_file",
]
