"""Two classes, one file: the image a session runs on, and the container itself.

Both own their own persistence. `Image.build` writes the environment row it
belongs to; `Sandbox.provision` writes the session's sandbox row and fetches
its own skills and files. Nothing above this layer signs a URL or knows where
an object lives in the bucket — callers name a file, and this decides how it
gets in or out.

Connecting to E2B is a billable call that resumes a paused container, so
nothing here connects up front. A `Sandbox` may know only its id; the first
call that touches it connects, and every later call re-validates — a container
that paused while the model was thinking heals itself instead of failing.
"""

from __future__ import annotations

import asyncio
import mimetypes
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from urllib.parse import urlparse
from typing import Any

import structlog
from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)
from e2b import AsyncSandbox, AsyncTemplate, CommandExitException
from httpcore import LocalProtocolError, NetworkError as HttpcoreNetworkError
from httpcore import ProtocolError as HttpcoreProtocolError
from httpx import TransportError as HttpxTransportError
from langchain_e2b import AsyncE2BSandbox
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Environment, File, Session, Skill
from app.db.models.environments import BUILDING, FAILED, READY
from app.db.queries import environments as environments_q
from app.db.queries import files as files_q
from app.db.queries import sessions as sessions_q
from app.db.queries import skills as skills_q
from app.utils import storage
from app.utils.timing import timed
from app.utils.volume import SandboxVolumeMount

# The base image, and the two facts that come with it: where work happens and
# which unprivileged user the agent runs as. Swapping the image means revisiting
# all three together, so they move as one — a code change, not configuration.
BASE_IMAGE = "base"
WORKDIR = "/home/user"
GUEST_USER = "user"

# The layout every sandbox gets, whether or not there is anything to put in it.
# All three exist from the start because the system prompt names them, and a
# path the agent is told about had better be there when it looks.
#
# The first two are written as root and left read-only: `skills` is read by the
# agent as instructions and `uploads` is what the user handed it, so neither is
# the agent's to rewrite. `outputs` is the opposite — it belongs to the agent,
# and we read it afterwards.
SKILLS_DIR = f"{WORKDIR}/skills"
UPLOADS_DIR = f"{WORKDIR}/uploads"
OUTPUTS_DIR = f"{WORKDIR}/outputs"
WEB_CACHE_DIR = f"{WORKDIR}/.web_cache" # store long,intermediate results returned by web_fetch

# How long a connection just proven live is taken at its word before being
# checked again. Long enough to cover a burst — copying one file in is two
# calls back to back — and short enough that `set_timeout`, which is both the
# liveness check and what stops an active container pausing, still runs often
# against anything doing real work.
_CONNECTION_FRESH_SECONDS = 15.0

# Compatibility layout consumed by the shared CMA/VMA Agent prompts and
# Skills.  VMA keeps its canonical E2B paths under /home/user; these aliases
# let one checked-in Agent contract address both providers without copying or
# rewriting skill content.
COMPAT_WORKDIR = "/workspace"
COMPAT_SKILLS_DIR = "/mnt/skills"
COMPAT_UPLOADS_DIR = "/mnt/session/uploads"
COMPAT_OUTPUTS_DIR = "/mnt/session/outputs"

# Order matters only in that apt comes first, so a later manager can rely on the
# toolchain it brings in.
PACKAGE_MANAGERS = ("apt", "cargo", "gem", "go", "npm", "pip")

# Skill bytes travel straight from object storage to the sandbox.  Parallelism
# therefore belongs inside one sandbox command, not in concurrent calls through
# one mutable E2B client.  Agent versions currently allow at most twenty Skills,
# so ten means one wave for Website Builder and at most two for any Agent.
SKILL_DOWNLOAD_CONCURRENCY = 10
SKILL_DOWNLOAD_ATTEMPTS = 3
SKILL_DOWNLOAD_CONNECT_TIMEOUT_SECONDS = 5
SKILL_DOWNLOAD_TIMEOUT_SECONDS = 30
FAILED_SANDBOX_CLEANUP_TIMEOUT_SECONDS = 10
E2B_TRANSPORT_RETRY_DELAY_SECONDS = 0.1

# E2B reaches its control plane through httpx and its command channel through
# httpcore, so the same dropped HTTP/2 connection surfaces from two exception
# families.  These are transport failures — deliberately not E2B API errors
# such as authentication, quota or a missing sandbox.
_E2B_TRANSPORT_ERRORS = (
    HttpxTransportError,
    HttpcoreNetworkError,
    HttpcoreProtocolError,
)

_SKILL_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

logger = structlog.get_logger(__name__)


# Everything, as a CIDR. The provider rejects a symbolic name here.
_ALL_TRAFFIC = "0.0.0.0/0"


def _storage_host() -> str | None:
    """The one host a network-restricted container still has to reach.

    Files move between object storage and a container over a signed URL the
    container fetches itself, so a container that can reach nothing can also
    be given nothing.
    """
    endpoint = get_settings().s3_endpoint_url
    if not endpoint:
        return None
    return urlparse(endpoint).hostname


@dataclass(frozen=True)
class BuildStatus:
    state: str  # building | ready | failed
    error: str | None = None


@dataclass(frozen=True)
class OutputFile:
    """One file the agent left in `outputs/`, as the container reports it."""

    path: str  # relative to OUTPUTS_DIR
    size_bytes: int
    sha256: str


def _skill_install_command(
    skills: list[tuple[Skill, str]],
    *,
    skills_dir: str = SKILLS_DIR,
    workdir: str = WORKDIR,
) -> str:
    """Build one fail-closed command for a whole Skill set.

    The command is intentionally a single E2B RPC.  Its bounded batches perform
    independent object downloads, while extraction remains serial and lands in
    a private staging tree.  No signed URL is printed on any failure path.
    """
    jobs: list[str] = []
    extracts: list[str] = []
    for index, (skill, url) in enumerate(skills):
        args = " ".join(
            shlex.quote(str(value))
            for value in (
                index,
                skill.name,
                skill.size_bytes,
                skill.sha256 or "",
                url,
            )
        )
        jobs.append(
            f"download_one {args} &\n"
            "pids+=(\"$!\")\n"
            f"if (( ${{#pids[@]}} == {SKILL_DOWNLOAD_CONCURRENCY} )); then\n"
            "  if ! wait_batch \"${pids[@]}\"; then download_failed=1; fi\n"
            "  pids=()\n"
            "fi"
        )
        extracts.append(
            "if ! extract_one "
            f"{shlex.quote(str(index))} {shlex.quote(skill.name)}; then exit 33; fi"
        )

    return f"""set -euo pipefail
skills_dir={shlex.quote(skills_dir)}
work="$(mktemp -d {shlex.quote(f'{workdir}/.skills-install.XXXXXX')})"
archives="$work/archives"
expanded="$work/expanded"
mkdir -p "$archives" "$expanded" "$work/headers" "$work/retries"
trap 'rm -rf "$work"' EXIT

download_one() {{
  local index="$1" name="$2" expected_size="$3" expected_sha="$4" url="$5"
  local archive part
  archive="$archives/$index.zip"
  part="$archive.part"
  local headers="$work/headers/$index" attempt=1 status=0 http_code=000
  local actual_size actual_sha retry_after delay

  while (( attempt <= {SKILL_DOWNLOAD_ATTEMPTS} )); do
    rm -f "$part" "$headers"
    if http_code="$(curl --silent --show-error --location \
      --connect-timeout {SKILL_DOWNLOAD_CONNECT_TIMEOUT_SECONDS} \
      --max-time {SKILL_DOWNLOAD_TIMEOUT_SECONDS} \
      --output "$part" --dump-header "$headers" \
      --write-out '%{{http_code}}' "$url" 2>/dev/null)"; then
      status=0
    else
      status=$?
    fi
    http_code="${{http_code:-000}}"

    if (( status == 0 )) && [[ "$http_code" == 2* ]]; then
      actual_size="$(wc -c < "$part" | tr -d '[:space:]')"
      if (( expected_size > 0 )) && [[ "$actual_size" != "$expected_size" ]]; then
        status=90
      elif [[ -n "$expected_sha" ]]; then
        if ! actual_sha="$(sha256sum "$part" | awk '{{print $1}}')"; then
          echo "Skill $name could not be checksummed" >&2
          return 1
        fi
        if [[ "$actual_sha" != "$expected_sha" ]]; then
          status=91
        fi
      fi
      if (( status == 0 )); then
        if ! mv "$part" "$archive"; then
          echo "Skill $name archive could not be staged" >&2
          return 1
        fi
        return 0
      fi
    elif (( status == 0 )); then
      case "$http_code" in
        408|425|429|5??) status=92 ;;
        *)
          echo "Skill $name download failed permanently (HTTP $http_code)" >&2
          return 1
          ;;
      esac
    fi

    if (( attempt == {SKILL_DOWNLOAD_ATTEMPTS} )); then
      echo "Skill $name download failed after $attempt attempts (HTTP $http_code, code $status)" >&2
      return 1
    fi

    touch "$work/retries/$index.$attempt"
    retry_after="$(awk 'BEGIN {{IGNORECASE=1}} /^Retry-After:/ {{gsub("\\r", "", $2); value=$2}} END {{print value}}' "$headers" 2>/dev/null || true)"
    if [[ "$retry_after" =~ ^[0-9]+$ ]]; then
      if (( retry_after > 10 )); then delay=10; else delay="$retry_after"; fi
    else
      delay="$attempt.$((RANDOM % 1000))"
    fi
    sleep "$delay"
    attempt=$((attempt + 1))
  done
  return 1
}}

wait_batch() {{
  local failed=0 pid
  for pid in "$@"; do
    if ! wait "$pid"; then failed=1; fi
  done
  return "$failed"
}}

extract_one() {{
  local index="$1" name="$2" archive="$archives/$1.zip"
  if ! unzip -tq "$archive" >/dev/null 2>&1; then
    echo "Skill $name archive is invalid" >&2
    return 1
  fi
  if ! unzip -oq "$archive" -d "$expanded"; then
    echo "Skill $name could not be extracted" >&2
    return 1
  fi
  if [[ ! -f "$expanded/$name/SKILL.md" ]]; then
    echo "Skill $name has no matching SKILL.md" >&2
    return 1
  fi
  chmod -R a+rX "$expanded/$name"
}}

pids=()
download_failed=0
{chr(10).join(jobs)}
if (( ${{#pids[@]}} > 0 )); then
  if ! wait_batch "${{pids[@]}}"; then download_failed=1; fi
fi
if (( download_failed != 0 )); then exit 31; fi

{chr(10).join(extracts)}

# prepare_directories created this path and nothing can use the Session until
# provisioning returns.  Replacing that empty directory from the same parent
# publishes the complete set together; any earlier failure only removes work.
if ! rmdir "$skills_dir" || ! mv "$expanded" "$skills_dir"; then
  echo "The complete Skill set could not be published" >&2
  exit 32
fi
retry_count="$(find "$work/retries" -type f | wc -l | tr -d '[:space:]')"
echo "skills_installed={len(skills)} retries=$retry_count"
"""


async def _resolve_skills(
    db: AsyncSession,
    *,
    skill_ids: list[str],
    organization_id: str,
) -> list[Skill]:
    requested_ids = list(dict.fromkeys(skill_ids))
    skills = await skills_q.get_skills_by_ids(
        db,
        skill_ids=requested_ids,
        organization_id=organization_id,
    )
    found = {skill.id for skill in skills}
    missing = [skill_id for skill_id in requested_ids if skill_id not in found]
    if missing:
        raise ValueError(f"Skill {missing[0]} does not exist")

    names: set[str] = set()
    for skill in skills:
        if _SKILL_NAME_PATTERN.fullmatch(skill.name) is None or len(skill.name) > 64:
            raise ValueError(f"Skill {skill.id} has an invalid name")
        if skill.name in names:
            raise ValueError(f"More than one Skill is named {skill.name}")
        names.add(skill.name)
    return skills


class Image:
    """The image an environment's sessions start from.

    An environment declares packages; this builds them into an E2B template
    once, so every session that uses it starts in about a second instead of
    installing the same things again. Nothing tells us when a build finishes,
    so the state in the environment row is refreshed by asking.
    """

    def __init__(self, image_id: str, build_id: str | None, name: str) -> None:
        self._image_id = image_id
        self._build_id = build_id
        self._name = name

    @property
    def image_id(self) -> str:
        """What `Sandbox.provision` starts from — E2B's own id, not our name."""
        return self._image_id

    @property
    def build_id(self) -> str | None:
        return self._build_id

    # ---- construction -----------------------------------------------------

    @classmethod
    async def build(
        cls,
        db: AsyncSession,
        environment: Environment,
        *,
        packages: dict[str, list[str]],
        cpu: int,
        memory_mb: int,
    ) -> Image:
        """Start a build and record it, without waiting for it to finish.

        Installing a real package set takes minutes, which no HTTP request
        should sit through. E2B caches build layers, so re-declaring the same
        packages is cheap.

        The image is named after the environment's id rather than the label the
        user chose: template names are global at the provider, and two
        organizations are free to pick the same label.

        `cpu` and `memory_mb` are fixed into the image, so every container
        started from it gets them — a headless browser and a spreadsheet script
        want very different machines.
        """
        builder = AsyncTemplate().from_base_image()
        for manager in PACKAGE_MANAGERS:
            entries = packages.get(manager) or []
            if entries:
                builder = _install(builder, manager, entries)

        build = await AsyncTemplate.build_in_background(
            builder,
            name=environment.id,
            cpu_count=cpu,
            memory_mb=memory_mb,
            api_key=get_settings().e2b_api_key,
        )
        image = cls(str(build.template_id), str(build.build_id), environment.id)
        await environments_q.set_build(
            db,
            environment,
            state=BUILDING,
            image_id=image.image_id,
            build_id=image.build_id,
        )
        return image

    @classmethod
    def base(cls) -> Image:
        """What an environment that declared nothing runs on.

        Written into the environment row at creation rather than worked out at
        sandbox-start time, so which image a session ran on is a stored fact.
        """
        return cls(BASE_IMAGE, None, BASE_IMAGE)

    @classmethod
    def from_environment(cls, environment: Environment) -> Image | None:
        """Rebuild the handle from what was stored. No network call."""
        if not environment.image_id:
            return None
        return cls(environment.image_id, environment.build_id, environment.id)

    # ---- build state ------------------------------------------------------

    async def refresh(self, db: AsyncSession, environment: Environment) -> BuildStatus:
        """Ask how the build went and write the answer down.

        Done on read rather than by a background loop: nothing calls us when a
        build finishes, and an environment nobody looks at does not need
        chasing.
        """
        status = await self.status()
        if status.state != BUILDING:
            await environments_q.set_build(
                db, environment, state=status.state, error=status.error
            )
        return status

    async def status(self) -> BuildStatus:
        from e2b.template.types import BuildInfo

        if not self._build_id:
            return BuildStatus(state=READY)

        result = await AsyncTemplate.get_build_status(
            BuildInfo(
                template_id=self._image_id,
                build_id=self._build_id,
                name=self._name,
                alias=self._name,
            ),
            api_key=get_settings().e2b_api_key,
        )
        # An enum, whose str() is "TemplateBuildStatus.READY" — the member name,
        # not the wire value. Reading it without unwrapping matches nothing, and
        # every build looks like it is still going, forever.
        reported = getattr(result, "status", "")
        state = str(getattr(reported, "value", reported) or "").lower()
        if state == "ready":
            return BuildStatus(state=READY)
        if state == "error":
            return BuildStatus(state=FAILED, error=_build_failure(result))
        if state in ("building", "waiting"):
            return BuildStatus(state=BUILDING)
        # Every state is spelled out, and anything else is a provider we no
        # longer understand. Treating the unknown as "still building" is how a
        # broken read turns into an environment that waits forever.
        raise RuntimeError(f"Unknown build status {state!r} from the sandbox provider")


class Sandbox:
    """One instance = one E2B container, bound to one scope.

    The scope is a Session for the containers an agent lives in, and a Sandbox
    row for the ones the API hands out directly. Everything below treats it as
    an opaque id: it labels the files this container produces and the timings
    it logs, and nothing here reads it for anything else.
    """

    def __init__(
        self,
        sandbox_id: str,
        scope_id: str,
        organization_id: str,
        native: AsyncSandbox | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._sandbox_id = sandbox_id
        self._scope_id = scope_id
        self._organization_id = organization_id
        self._native = native
        # How long E2B is told to keep this container after each call. A
        # Session container uses the deployment-wide default; one created
        # through the API carries whatever TTL its caller asked for, which is
        # the whole reason this is per-instance rather than read from settings
        # at the point of use.
        self._ttl_seconds = ttl_seconds or get_settings().sandbox_timeout_seconds
        # When this handle was last proven live. None for a handle handed in
        # rather than opened here — it gets checked once like any other.
        self._verified_at: float | None = None

    @property
    def sandbox_id(self) -> str:
        return self._sandbox_id

    @property
    def scope_id(self) -> str:
        return self._scope_id

    @property
    def session_id(self) -> str:
        """The scope, for the Session callers that only ever have one."""
        return self._scope_id

    @property
    def organization_id(self) -> str:
        return self._organization_id

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    @property
    def to_deep_agent_backend(self) -> LazyE2BBackend:
        """Wrap this for `create_deep_agent(backend=...)`. Still no network call."""
        return LazyE2BBackend(self)

    # ---- construction -----------------------------------------------------

    @classmethod
    async def provision(
        cls,
        db: AsyncSession,
        session: Session,
        *,
        image: Image,
        skill_ids: list[str],
        files: list[tuple[str, str]],
        memory_mounts: list[SandboxVolumeMount] | None = None,
    ) -> Sandbox:
        """Start the container a session will live in, and fill it.

        `files` is `(file_id, path)` pairs, each path relative to `uploads/`.
        Memory Stores are different: E2B mounts their provider Volumes while
        creating the container, before anything inside it can run.
        Whatever the environment declared is already in the image, so nothing
        is installed here — which is what keeps this to about a second.

        Done at session creation rather than on first message, so a session
        either works from the start or fails while the user is still looking at
        it. Nothing ever creates a second one: if the container is gone by the
        time a turn runs, the session is finished, not repaired.
        """
        skills = (
            await _resolve_skills(
                db,
                skill_ids=skill_ids,
                organization_id=session.organization_id,
            )
            if skill_ids
            else []
        )
        native = await AsyncSandbox.create(
            template=image.image_id,
            timeout=get_settings().sandbox_timeout_seconds,
            api_key=get_settings().e2b_api_key,
            volume_mounts={
                mount.mount_path: mount.volume_name
                for mount in (memory_mounts or [])
            }
            or None,
            # An idle container pauses instead of dying, keeping its filesystem
            # and nothing else. Commands run one at a time here, so there is no
            # process worth the price of a memory snapshot. Waking it is
            # `ensure_connected`.
            lifecycle={"on_timeout": {"action": "pause", "keep_memory": False}},
        )
        sandbox = cls(native.sandbox_id, session.id, session.organization_id, native=native)
        try:
            await sandbox.prepare_directories()
            if skills:
                await sandbox._install_skills(skills)
            for file_id, path in files:
                await sandbox.upload_file(db, f"{UPLOADS_DIR}/{path}", file_id)

            ttl_seconds = get_settings().sandbox_timeout_seconds
            row = await sessions_q.create_sandbox(
                db,
                session,
                provider="e2b",
                external_sandbox_id=native.sandbox_id,
                ttl_seconds=ttl_seconds,
                # A session's container reaches the network. Recorded rather
                # than assumed, so the row says what was actually allowed.
                network_access=True,
                # Not consulted before a turn: this is when E2B would pause an
                # idle container, which is not when the container dies.
                expires_at=datetime.now(timezone.utc)
                + timedelta(seconds=ttl_seconds),
            )
            await sessions_q.update_sandbox_state(db, row, state="running")
            return sandbox
        except BaseException as primary:
            # Until create_sandbox() commits, the janitor has no row from which
            # it could discover this external container.  A provisioning
            # failure therefore has to destroy the native sandbox here.  The
            # cleanup is bounded and can never hide the error the caller needs.
            try:
                await asyncio.wait_for(
                    native.kill(),
                    timeout=FAILED_SANDBOX_CLEANUP_TIMEOUT_SECONDS,
                )
            except BaseException as cleanup_error:
                logger.warning(
                    "failed_sandbox_cleanup",
                    session_id=session.id,
                    sandbox_id=native.sandbox_id,
                    primary_error=type(primary).__name__,
                    cleanup_error=type(cleanup_error).__name__,
                )
            raise

    @classmethod
    async def start(
        cls,
        *,
        image: Image,
        scope_id: str,
        organization_id: str,
        ttl_seconds: int,
        network_access: bool = False,
    ) -> Sandbox:
        """Start a bare container, with none of a Session's furniture.

        `provision` above fills a container with skills, uploads and the
        directory layout an agent's prompt names. Nothing here needs any of
        that: a caller reaching this through the API brings its own files and
        runs its own command, so the container it gets is the image and
        nothing else.

        The container dies on its own when the TTL runs out. That is what
        bounds a leak — a caller that never deletes, or a process that stops
        before it can, costs `ttl_seconds` of container and not a cent more —
        and it is why the TTL is written into E2B here rather than enforced by
        a reaper of ours that could itself be the thing that died.

        Network is off unless asked for. A container that can both run
        arbitrary commands and reach the internet is an exfiltration path, and
        the great majority of what runs here — linting, converting, checking —
        never needs to leave the machine.

        "Off" is one allowed host rather than nothing at all, because getting
        a file in or out is the container fetching a signed URL from object
        storage. Cutting egress completely would not make the container safer,
        it would make it useless: the only way left to hand it a file would be
        to stream the bytes through this process, which is exactly what the
        signed URL exists to avoid. So the storage host is reachable and
        nothing else is, which is the property that was actually wanted.
        """
        network: dict[str, Any] | None = None
        if not network_access:
            allowed = _storage_host()
            # The provider requires the deny to be explicit: an allow list on
            # its own permits everything it does not mention.
            network = {
                "allow_out": [allowed] if allowed else [],
                "deny_out": [_ALL_TRAFFIC],
            }

        native = await AsyncSandbox.create(
            template=image.image_id,
            timeout=ttl_seconds,
            api_key=get_settings().e2b_api_key,
            # Egress is shaped by `network` below; switching this off would
            # take the storage host with it.
            allow_internet_access=True,
            network=network,
        )
        return cls(
            native.sandbox_id,
            scope_id,
            organization_id,
            native=native,
            ttl_seconds=ttl_seconds,
        )

    @classmethod
    def from_id(
        cls,
        sandbox_id: str,
        scope_id: str,
        organization_id: str,
        ttl_seconds: int | None = None,
    ) -> Sandbox:
        """Reference a known container without connecting. No network call."""
        return cls(sandbox_id, scope_id, organization_id, ttl_seconds=ttl_seconds)

    @classmethod
    async def connect(
        cls,
        sandbox_id: str,
        scope_id: str,
        organization_id: str,
        ttl_seconds: int | None = None,
    ) -> Sandbox:
        """Reference a known container and connect right away.

        For callers that need a live connection immediately — tests, one-off
        scripts. Turn execution prefers `from_id` and connects lazily.
        """
        sandbox = cls.from_id(sandbox_id, scope_id, organization_id, ttl_seconds)
        await sandbox.ensure_connected()
        return sandbox

    # ---- connection -------------------------------------------------------

    async def ensure_connected(self) -> None:
        """Guarantee a live connection, paying for one only when needed.

        Both branches are timed and told apart by `reused`, because they are
        not the same expense and the cheap-looking one is not free: keeping an
        existing connection still costs a `set_timeout`, a round trip to E2B,
        and this runs before *every* backend call.

        Which is why a connection just proven live is taken at its word for a
        few seconds. Calls arrive in bursts — copying one file in is two of
        them back to back, and mounting three attachments was six — and asking
        E2B six times in as many seconds whether a container is still there
        buys nothing the first answer did not. A handle that goes stale inside
        the window is still handled: `run` drops it and reconnects.

        The window also bounds how long `set_timeout` can go uncalled, which is
        what keeps a busy container from pausing underneath a turn.
        """
        if (
            self._native is not None
            and self._verified_at is not None
            and time.monotonic() - self._verified_at < _CONNECTION_FRESH_SECONDS
        ):
            return
        async with timed(
            "sandbox_connected", scope_id=self._scope_id, sandbox_id=self._sandbox_id
        ) as span:
            if self._native is not None:
                try:
                    # No `is_running()` first. Asking and then acting is two
                    # round trips where acting alone is one, and this runs
                    # before every backend call — measured, the "cheap" branch
                    # cost more than reconnecting outright. `set_timeout` is
                    # the liveness check: a container that has gone says so.
                    await self._native.set_timeout(self._ttl_seconds)
                    span["reused"] = True
                    self._verified_at = time.monotonic()
                    return
                except Exception as exc:
                    # Not swallowed: the reconnect below either succeeds or
                    # raises in its place. Recorded so a container that is
                    # being rebuilt on every call is visible rather than
                    # merely slow.
                    span["stale"] = type(exc).__name__
            span["reused"] = False
            for attempt in (1, 2):
                try:
                    self._native = await AsyncSandbox.connect(
                        self._sandbox_id,
                        timeout=self._ttl_seconds,
                        api_key=get_settings().e2b_api_key,
                    )
                    break
                except _E2B_TRANSPORT_ERRORS as exc:
                    if attempt == 2:
                        raise
                    # Connecting only obtains a handle and extends the TTL. It
                    # is safe to repeat even when the first response was lost.
                    self._native = None
                    self._verified_at = None
                    span["transport_retry"] = type(exc).__name__
                    await asyncio.sleep(E2B_TRANSPORT_RETRY_DELAY_SECONDS)
            self._verified_at = time.monotonic()

    async def run(
        self, command: str, *, idempotent: bool = False, **kwargs: Any
    ) -> ExecuteResponse:
        """Run a command in the container.

        The command itself is not logged — an agent's shell line can carry
        anything the user put in front of it. Its length and the exit code say
        enough to tell one call from another when reading timings back.

        A `LocalProtocolError` is always retried once. It comes out
        of the HTTP/2 layer inside E2B's own client — "invalid input RECV_PING
        in state CLOSED" — and means the connection this request was about to
        go out on had already been closed by the far end. The request never
        left the process, so sending it again cannot repeat anything.

        Read/write transport errors are different: a command may have reached
        the sandbox before its connection disappeared. They are retried only
        when the caller marks the command idempotent. File hashes, listings and
        PUTs use that path; arbitrary agent shell commands never do.

        It is rare — once in fifty-odd sandbox starts — and it surfaced as a
        500 on `POST /v1/sessions`, because provisioning runs commands and
        nothing above this layer retries.
        """
        kwargs.setdefault("user", GUEST_USER)
        kwargs.setdefault("timeout", get_settings().sandbox_command_timeout_seconds)
        async with timed(
            "sandbox_command",
            scope_id=self._scope_id,
            command_chars=len(command),
        ) as span:
            for attempt in (1, 2):
                await self.ensure_connected()
                try:
                    result = await self._native.commands.run(command, **kwargs)
                except LocalProtocolError:
                    if attempt == 2:
                        raise
                    # Drop the handle so `ensure_connected` builds a fresh
                    # client rather than reaching into the same dead pool.
                    # Clearing the freshness stamp with it is what stops the
                    # retry from being waved through as recently verified.
                    self._native = None
                    self._verified_at = None
                    span["retried"] = "stale_connection"
                    continue
                except _E2B_TRANSPORT_ERRORS as exc:
                    if not idempotent or attempt == 2:
                        raise
                    self._native = None
                    self._verified_at = None
                    span["retried"] = f"idempotent_{type(exc).__name__}"
                    await asyncio.sleep(E2B_TRANSPORT_RETRY_DELAY_SECONDS)
                    continue
                span["exit_code"] = getattr(result, "exit_code", None)
                return result

    # ---- lifecycle --------------------------------------------------------

    async def pause(self) -> None:
        await self.ensure_connected()
        await self._native.pause(keep_memory=False)

    async def kill(self) -> None:
        await self.ensure_connected()
        await self._native.kill()

    # ---- layout -----------------------------------------------------------

    async def prepare_directories(self) -> None:
        """Lay out the canonical directories and shared-contract aliases.

        The system prompt names all of them, so all of them have to exist — an
        agent told to write to `outputs/` should not have to create it, and an
        empty `uploads/` says "nothing was attached" far more clearly than a
        missing one.

        `outputs` belongs to the agent; the other two are filled by us and left
        read-only, so they stay owned by root.  The aliases are created as root
        and point only at these canonical paths. `ln -T` deliberately fails if
        a base image ever puts a real directory at an alias path; silently
        merging two layouts would make resource isolation ambiguous.
        """
        result = await self.run(
            f"mkdir -p {shlex.quote(SKILLS_DIR)} {shlex.quote(UPLOADS_DIR)} "
            f"{shlex.quote(OUTPUTS_DIR)} "
            f"&& chmod 755 {shlex.quote(SKILLS_DIR)} {shlex.quote(UPLOADS_DIR)} "
            f"&& chown {GUEST_USER}:{GUEST_USER} {shlex.quote(OUTPUTS_DIR)} "
            "&& mkdir -p /mnt/session "
            f"&& ln -sfnT {shlex.quote(WORKDIR)} {shlex.quote(COMPAT_WORKDIR)} "
            f"&& ln -sfnT {shlex.quote(SKILLS_DIR)} {shlex.quote(COMPAT_SKILLS_DIR)} "
            f"&& ln -sfnT {shlex.quote(UPLOADS_DIR)} {shlex.quote(COMPAT_UPLOADS_DIR)} "
            f"&& ln -sfnT {shlex.quote(OUTPUTS_DIR)} {shlex.quote(COMPAT_OUTPUTS_DIR)}",
            user="root",
        )
        exit_code = getattr(result, "exit_code", 0)
        if exit_code != 0:
            # E2B returns ordinary command failures instead of raising.  A
            # half-created layout is unusable because prompts and shared
            # skills address the compatibility paths directly.
            raise RuntimeError(
                "The sandbox compatibility layout could not be prepared "
                f"(exit code {exit_code})"
            )

    # ---- files ------------------------------------------------------------
    # Bytes move between the container and the bucket directly, in both
    # directions. The container is handed one short-lived URL for one object —
    # never file content through this process, never a standing credential.

    async def upload_file(self, db: AsyncSession, path: str, file_id: str) -> None:
        """Fetch a stored file into the container at `path`, read-only.

        Read-only because these are the user's inputs: an agent that edited one
        would leave the client's own copy silently disagreeing with what the
        run actually used.
        """
        file = await files_q.get_file(
            db, file_id=file_id, organization_id=self._organization_id
        )
        if file is None:
            raise ValueError(f"File {file_id} does not exist")

        url = await storage.presigned_download_url(
            file.storage_key, expires_in=get_settings().transfer_url_ttl_seconds
        )
        parent = str(PurePosixPath(path).parent)
        result = await self.run(
            f"mkdir -p {shlex.quote(parent)} "
            f"&& curl -fsSL -o {shlex.quote(path)} {shlex.quote(url)} "
            f"&& chmod 444 {shlex.quote(path)}",
            user="root",
        )
        exit_code = getattr(result, "exit_code", 0)
        if exit_code != 0:
            # E2B reports ordinary command failures in the result rather than
            # raising them. Treating a failed curl as success would let the API
            # commit a Session resource whose bytes never reached the sandbox.
            raise RuntimeError(
                f"The sandbox could not fetch File {file_id} (exit code {exit_code})"
            )

    async def download_file(
        self,
        db: AsyncSession,
        path: str,
        *,
        scope_id: str | None = None,
        filename: str | None = None,
        sha256: str | None = None,
    ) -> File:
        """Push a file out of the container into storage, as a file record.

        `scope_id` marks which session produced it, which is how a caller later
        asks for one session's output rather than everything in the account.

        Taking the same unchanged file twice returns the record from the first
        time rather than making another. That is what lets this be called
        whenever anyone wants the file — after a turn, from a tool, twice by a
        client that retried — without the row count following them around.

        `sha256` is accepted from a caller that has already computed it, since
        listing a directory computes them all in one go anyway.
        """
        name = filename or path.rsplit("/", 1)[-1]
        digest = sha256 or await self._digest(path)
        if digest is None:
            raise ValueError(f"{path} does not exist in the sandbox")

        if scope_id is not None:
            previous = await files_q.get_latest_scoped_file(
                db,
                scope_id=scope_id,
                filename=name,
                organization_id=self._organization_id,
            )
            if previous is not None and previous.sha256 == digest:
                return previous

        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        key = storage.object_key(
            organization_id=self._organization_id,
            category="outputs",
            filename=name,
            content_sha256=digest,
        )
        url = await storage.presigned_upload_url(
            key, mime_type=content_type, expires_in=get_settings().transfer_url_ttl_seconds
        )
        await self.run(
            f"curl -fsS -X PUT -H {shlex.quote(f'Content-Type: {content_type}')} "
            f"-T {shlex.quote(path)} {shlex.quote(url)}",
            idempotent=True,
            user="root",
        )

        # What the bucket received, not what the container claimed to send. The
        # record is written last, so it never describes a file that is not there.
        size = await storage.object_size(key)
        if size is None:
            raise ValueError(f"{path} never arrived in storage")
        return await files_q.create_file(
            db,
            organization_id=self._organization_id,
            filename=name,
            storage_key=key,
            mime_type=content_type,
            size_bytes=size,
            sha256=digest,
            scope_id=scope_id,
        )

    async def _digest(self, path: str) -> str | None:
        result = await self.run(
            f"sha256sum {shlex.quote(path)} 2>/dev/null || true",
            idempotent=True,
        )
        digest = (result.stdout or "").split(" ", 1)[0].strip()
        return digest or None

    async def read_bytes(self, path: str, *, max_bytes: int) -> bytes:
        """One file, out of the container and into this process.

        Not `download_file`: that one moves bytes to the bucket and writes a
        row, because it is how a deliverable leaves. This is for looking at a
        file, and leaves nothing behind.

        The size is checked in the container first. A ceiling enforced after
        the transfer is a ceiling that costs the transfer.
        """
        measured = await self.run(
            f"stat -c %s {shlex.quote(path)} 2>/dev/null || echo -1",
            idempotent=True,
        )
        raw = (measured.stdout or "").strip()
        size = int(raw) if raw.lstrip("-").isdigit() else -1
        if size < 0:
            raise FileNotFoundError(path)
        if size > max_bytes:
            raise ValueError(f"{path} is {size} bytes, over the {max_bytes} byte limit")

        await self.ensure_connected()
        return bytes(await self._native.files.read(path, format="bytes", user=GUEST_USER))

    async def write_bytes(self, path: str, data: bytes) -> None:
        """The write-side mirror of `read_bytes`: one file, into the container.

        For callers outside the agent's own tool loop that need to put content
        into the sandbox — currently just `web_fetch`'s overflow path. Creates
        the parent directory first, since nothing before this ever wrote here.
        """
        parent = str(PurePosixPath(path).parent)
        await self.run(f"mkdir -p {shlex.quote(parent)}")
        await self.ensure_connected()
        await self._native.files.write(path, data, user=GUEST_USER)

    async def list_files(
        self,
        path: str,
        *,
        max_files: int | None = None,
        include_oversized: bool = False,
    ) -> list[OutputFile]:
        """Everything under `path`, with a hash, as the container sees it.

        The hash is what makes collection repeatable: it says whether a file is
        the same one already taken, where a timestamp only says it was touched.

        Filenames containing a newline are skipped rather than guessed at —
        they would break the line-by-line reading of this output, and nothing
        good comes of misreading a path.
        """
        # Size and hash in the same command. They used to be two: a `find` for
        # the hashes and then a `stat` per file, which is one round trip per
        # deliverable on top of the one that found them. Over this network a
        # round trip is most of a second.
        file_limit = max_files or get_settings().max_output_files
        result = await self.run(
            f"cd {shlex.quote(path)} 2>/dev/null "
            f"&& find . -type f 2>/dev/null | head -n {file_limit} "
            "| while IFS= read -r f; do "
            'printf "%s " "$(stat -c %s "$f" 2>/dev/null || echo -1)"; '
            'sha256sum "$f" 2>/dev/null; done',
            idempotent=True,
        )
        # Each line is `<size> <sha256>  ./<path>`; `stat` reports -1 for a file
        # that vanished between being listed and being read, which then fails
        # the size check below like any other unusable entry.
        found: list[OutputFile] = []
        for line in (result.stdout or "").splitlines():
            raw_size, _, rest = line.partition(" ")
            digest, _, relative = rest.partition("  ")
            relative = relative.strip().removeprefix("./")
            if not digest or not relative or not raw_size.lstrip("-").isdigit():
                continue
            size = int(raw_size)
            if size < 0:
                continue
            if not include_oversized and size > get_settings().max_output_bytes:
                continue
            found.append(OutputFile(path=relative, size_bytes=size, sha256=digest))
        return found

    async def discover_outputs(self, db: AsyncSession) -> list[File]:
        """Take everything new the agent left in `outputs/`.

        A session runs many turns and this runs after each of them, so a file
        that has not changed since it was last taken is left alone.

        A file the agent rewrote becomes another record rather than replacing
        the one before it. Records are only ever added: an id handed out to a
        client, or quoted back to a user, keeps working for as long as the file
        exists, whatever the agent does to the path afterwards.
        """
        async with timed("outputs_discovered", scope_id=self._scope_id) as span:
            found = await self.list_files(OUTPUTS_DIR)
            span["listed"] = len(found)

        collected: list[File] = []
        for output in found:
            before = await files_q.get_latest_scoped_file(
                db,
                scope_id=self._scope_id,
                filename=output.path,
                organization_id=self._organization_id,
            )
            if before is not None and before.sha256 == output.sha256:
                continue
            collected.append(
                await self.download_file(
                    db,
                    f"{OUTPUTS_DIR}/{output.path}",
                    scope_id=self._scope_id,
                    filename=output.path,
                    sha256=output.sha256,
                )
            )
        return collected

    # ---- skills -----------------------------------------------------------

    async def install_skills(self, db: AsyncSession, skill_ids: list[str]) -> None:
        """Fetch a complete Skill set concurrently and publish it together.

        Every stored package already carries its own `<name>/` directory, so
        serial extraction into one staging root gives DeepAgents the layout it
        scans, `<source>/<name>/SKILL.md`. Downloads run in bounded batches
        inside one E2B command: concurrent `self.run()` calls would race
        on this Sandbox's shared connection and would keep all of the command
        round trips this change is meant to remove.
        """
        skills = await _resolve_skills(
            db,
            skill_ids=skill_ids,
            organization_id=self._organization_id,
        )
        await self._install_skills(skills)

    async def _install_skills(self, skills: list[Skill]) -> None:
        signed: list[tuple[Skill, str]] = []
        for skill in skills:
            url = await storage.presigned_download_url(
                skill.storage_key,
                expires_in=get_settings().transfer_url_ttl_seconds,
            )
            signed.append((skill, url))

        async with timed(
            "skills_installed",
            session_id=self._scope_id,
            skill_count=len(skills),
            download_concurrency=min(SKILL_DOWNLOAD_CONCURRENCY, len(skills)),
        ) as span:
            try:
                result = await self.run(_skill_install_command(signed), user="root")
            except CommandExitException as exc:
                span["exit_code"] = exc.exit_code
                # The SDK exception includes command stderr. Keep provider
                # details (and any future curl formatting) out of the API.
                raise RuntimeError(
                    "The sandbox could not install its complete Skill set "
                    f"(exit code {exc.exit_code})"
                ) from None
            exit_code = getattr(result, "exit_code", None)
            span["exit_code"] = exit_code
            summary = re.search(
                r"\bskills_installed=(\d+) retries=(\d+)\b",
                getattr(result, "stdout", ""),
            )
            if summary is not None:
                span["download_retries"] = int(summary.group(2))
            if exit_code != 0:
                # E2B returns ordinary command failures instead of raising.
                # Never commit a Session whose sandbox only has some Skills.
                raise RuntimeError(
                    "The sandbox could not install its complete Skill set "
                    f"(exit code {exit_code})"
                )


def _install(builder: Any, manager: str, entries: list[str]) -> Any:
    if manager == "apt":
        return builder.apt_install(entries)
    if manager == "npm":
        return builder.npm_install(entries, g=True)
    if manager == "pip":
        return builder.pip_install(entries)
    # cargo, gem and go have no dedicated builder step.
    command = {"cargo": "cargo install", "gem": "gem install", "go": "go install"}[manager]
    return builder.run_cmd(f"{command} {' '.join(shlex.quote(e) for e in entries)}")


def _build_failure(result: Any) -> str:
    """Pull whatever the build said before it gave up."""
    reason = getattr(result, "reason", None)
    if reason:
        return str(reason)
    logs = getattr(result, "logs", None) or []
    tail = [str(getattr(entry, "message", entry)) for entry in logs[-20:]]
    return "\n".join(tail) or "the image build failed"


class LazyE2BBackend(SandboxBackendProtocol):
    """DeepAgents backend that connects on first use and self-heals afterward.

    `AsyncE2BSandbox` wants an already-connected container and never checks it
    again — fine for one call, wrong for a whole turn, where the model can take
    minutes between tool calls and the container can pause underneath it. Every
    call here reconnects first, and the delegate is rebuilt whenever the
    underlying connection was replaced.

    Every asynchronous method on the protocol is forwarded, not just the ones
    the agent's own tools use. The base class answers anything unforwarded with
    `NotImplementedError`, and DeepAgents calls more of it than the tools do —
    `SkillsMiddleware` reads each `SKILL.md` through `adownload_files` before
    the model is called, so leaving that one out meant skills installed
    correctly and then never reached the model at all. `test_backend_forwards_
    every_async_protocol_method` is what keeps this list honest.
    """

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox
        self._delegate: AsyncE2BSandbox | None = None
        self._delegate_native: AsyncSandbox | None = None

    async def _ready(self) -> AsyncE2BSandbox:
        await self._sandbox.ensure_connected()
        native = self._sandbox._native
        if self._delegate is None or self._delegate_native is not native:
            self._delegate = AsyncE2BSandbox(
                sandbox=native,
                workdir=WORKDIR,
                timeout=get_settings().sandbox_timeout_seconds,
            )
            self._delegate_native = native
        return self._delegate

    @property
    def id(self) -> str:
        return self._sandbox.sandbox_id

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return await (await self._ready()).aexecute(command, timeout=timeout)

    async def als(self, path: str) -> LsResult:
        return await (await self._ready()).als(path)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return await (await self._ready()).aread(file_path, offset=offset, limit=limit)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return await (await self._ready()).awrite(file_path, content)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        backend = await self._ready()
        return await backend.aedit(file_path, old_string, new_string, replace_all=replace_all)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        return await (await self._ready()).aglob(pattern, path=path)

    async def agrep(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> GrepResult:
        return await (await self._ready()).agrep(pattern, path=path, glob=glob)

    async def agrep_raw(self, *args: Any, **kwargs: Any) -> Any:
        return await (await self._ready()).agrep_raw(*args, **kwargs)

    # Batch reads and writes. Not the agent's tools — these are what the
    # middleware uses, `SkillsMiddleware` above all.

    async def adownload_files(self, paths: list[str]) -> Any:
        return await (await self._ready()).adownload_files(paths)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> Any:
        return await (await self._ready()).aupload_files(files)

    # Deprecated in DeepAgents but still reachable; forwarded so that reaching
    # them is a deprecation warning rather than a crash.

    async def als_info(self, path: str) -> Any:
        return await (await self._ready()).als_info(path)

    async def aglob_info(self, *args: Any, **kwargs: Any) -> Any:
        return await (await self._ready()).aglob_info(*args, **kwargs)


__all__ = [
    "BASE_IMAGE",
    "OUTPUTS_DIR",
    "SKILLS_DIR",
    "UPLOADS_DIR",
    "WEB_CACHE_DIR",
    "WORKDIR",
    "BuildStatus",
    "Image",
    "LazyE2BBackend",
    "OutputFile",
    "Sandbox",
]
