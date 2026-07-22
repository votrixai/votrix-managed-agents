"""One class, one file: everything about an E2B sandbox for a VMA session."""

from __future__ import annotations

import mimetypes
import shlex
from typing import Any

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
from e2b import AsyncSandbox
from langchain_e2b import AsyncE2BSandbox
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.queries import resources as res_q
from app.db.queries.session_sandboxes import upsert_session_sandbox
from app.storage import (
    create_presigned_download_url,
    create_presigned_upload_url,
    get_file_info,
    is_object_storage_backend,
    object_key,
    object_storage_backend_label,
)


class Sandbox:
    """One instance = one E2B sandbox, bound to one VMA session.

    Connecting to E2B is a real API call that resumes (and bills for) a
    paused sandbox, so ``Sandbox`` never does it up front. A freshly built
    instance may only know its ``sandbox_id`` — the first call that actually
    touches the sandbox runs ``ensure_connected()``, and every later call
    re-validates the connection before using it, so a sandbox that auto-paused
    mid-turn (the model took a while between tool calls) heals itself instead
    of failing.
    """

    def __init__(
        self,
        sandbox_id: str,
        session_id: str,
        organization_id: str,
        guest_user: str,
        timeout_seconds: int,
        native: AsyncSandbox | None = None,
    ) -> None:
        self._sandbox_id = sandbox_id
        self._session_id = session_id
        self._organization_id = organization_id
        self._guest_user = guest_user
        self._timeout_seconds = timeout_seconds
        self._native = native

    @property
    def sandbox_id(self) -> str:
        return self._sandbox_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def organization_id(self) -> str:
        return self._organization_id

    @property
    def to_deep_agent_backend(self) -> LazyE2BBackend:
        """Wrap this sandbox so it can be passed as create_deep_agent(backend=...).

        Building this makes no network call. The connection happens lazily,
        the first time the agent actually calls a filesystem/execute tool.
        """
        return LazyE2BBackend(self)

    # ---- construction: knows about VMA sessions/db ----------------------

    @classmethod
    async def provision(
        cls,
        db: AsyncSession,
        session_id: str,
        organization_id: str,
        template: str,
        skill_refs: list[dict[str, Any]],
    ) -> Sandbox:
        """Create a brand-new E2B sandbox, persist its id, and install skills.

        Called once, the first time a session needs a sandbox. ``template`` is
        an E2B template name (or ``name:tag``); ``skill_refs`` is the
        session's ``EffectiveAgentVersion.skills`` list — callers choose both,
        since they are per-session decisions, not global settings.
        """
        settings = get_settings()
        native = await AsyncSandbox.create(
            template=template,
            timeout=settings.vma_e2b_timeout_seconds,
            api_key=settings.e2b_api_key,
            lifecycle={
                "on_timeout": {
                    "action": "pause",
                    "keep_memory": settings.vma_e2b_keep_memory,
                },
                "auto_resume": settings.vma_e2b_auto_resume,
            },
        )
        await upsert_session_sandbox(
            db,
            session_id=session_id,
            provider="e2b",
            state="active",
            external_sandbox_id=native.sandbox_id,
            template_id=template,
            organization_id=organization_id,
        )
        sandbox = cls(
            native.sandbox_id,
            session_id,
            organization_id,
            settings.vma_e2b_guest_user,
            settings.vma_e2b_timeout_seconds,
            native=native,
        )
        if skill_refs:
            await sandbox.install_skills(db, skill_refs)
        return sandbox

    @classmethod
    def from_id(cls, sandbox_id: str, session_id: str, organization_id: str) -> Sandbox:
        """Reference an already-known E2B sandbox id without connecting yet.

        Pure local construction, no network call. The caller looks up
        ``sandbox_id`` (e.g. via ``get_session_sandbox().external_sandbox_id``)
        — this classmethod does not touch the database or E2B. Call this once
        per turn; the actual connection happens lazily on first use.
        """
        settings = get_settings()
        return cls(
            sandbox_id,
            session_id,
            organization_id,
            settings.vma_e2b_guest_user,
            settings.vma_e2b_timeout_seconds,
        )

    @classmethod
    async def connect(cls, sandbox_id: str, session_id: str, organization_id: str) -> Sandbox:
        """Reference an already-known E2B sandbox id and connect immediately.

        Convenience for callers that need a live connection right away (tests,
        one-off scripts). Turn execution should prefer ``from_id`` and let the
        connection happen lazily.
        """
        sandbox = cls.from_id(sandbox_id, session_id, organization_id)
        await sandbox.ensure_connected()
        return sandbox

    # ---- connection freshness ----------------------------------------------

    async def ensure_connected(self) -> None:
        """Make sure ``self._native`` is a live, unexpired connection.

        Connecting is a real E2B API call that resumes a paused sandbox (and
        is billed for it), so this only pays that cost when needed: never
        connected yet, or the held connection is no longer running. Otherwise
        it just extends the timeout on the connection already held.
        """
        if self._native is not None and await self._native.is_running():
            await self._native.set_timeout(self._timeout_seconds)
            return
        settings = get_settings()
        self._native = await AsyncSandbox.connect(
            self._sandbox_id,
            timeout=self._timeout_seconds,
            api_key=settings.e2b_api_key,
        )

    # ---- lifecycle --------------------------------------------------------

    async def pause(self) -> None:
        await self.ensure_connected()
        await self._native.pause(keep_memory=get_settings().vma_e2b_keep_memory)

    async def kill(self) -> None:
        await self.ensure_connected()
        await self._native.kill()

    # ---- files --------------------------------------------------------------
    # Files live in R2. The sandbox fetches its own file — VMA only ever
    # hands it a short-lived, single-object presigned URL, never a byte of
    # file content and never a standing credential.

    async def upload_file(self, db: AsyncSession, path: str, file_id: str) -> None:
        """Have the sandbox pull ``file_id`` from R2 straight into ``path``."""
        file = await res_q.get_resource(
            db, resource_id=file_id, resource_type="file", organization_id=self._organization_id
        )
        if file is None or not is_object_storage_backend(file.storage_backend) or not file.storage_key:
            raise ValueError(f"File {file_id} is not available in object storage")

        url = await create_presigned_download_url(file.storage_key)
        await self.run(f"curl -fsSL -o {shlex.quote(path)} {shlex.quote(url)}", user="root")

    async def download_file(self, db: AsyncSession, path: str) -> str:
        """Push the sandbox file at ``path`` into R2 and register it as a new file_id."""
        filename = path.rsplit("/", 1)[-1]
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        key = object_key(
            namespace=f"sessions_{self._session_id}",
            category="outputs",
            filename=filename,
            organization_id=self._organization_id,
        )

        url = await create_presigned_upload_url(key, content_type)
        await self.run(
            f"curl -fsS -X PUT -H {shlex.quote(f'Content-Type: {content_type}')} "
            f"-T {shlex.quote(path)} {shlex.quote(url)}",
            user="root",
        )

        info = await get_file_info(key)
        resource = await res_q.create_resource(
            db,
            resource_type="file",
            filename=filename,
            storage_backend=object_storage_backend_label(),
            storage_key=key,
            content_type=content_type,
            size_bytes=info.get("ContentLength"),
            organization_id=self._organization_id,
        )
        return resource.id

    async def discover_outputs(self, db: AsyncSession, *, max_files: int = 100) -> list[str]:
        """Register every file under the session's output directory as a file resource.

        Every call re-registers everything currently under the output root —
        it does not track what a prior call already discovered, so calling
        this more than once for an unchanged set of output files creates
        duplicate file resources. Callers that need idempotency across
        retries/recovery need to de-duplicate on their end for now.
        """
        root = f"{get_settings().vma_e2b_workdir}/outputs"
        result = await self.run(f"find {shlex.quote(root)} -type f 2>/dev/null | head -n {int(max_files)}")
        paths = [line for line in result.stdout.splitlines() if line.strip()]
        return [await self.download_file(db, path) for path in paths]

    # ---- skills -----------------------------------------------------------
    # Skills are zip archives in R2 and can contain executable helper files,
    # so — unlike memory — they have to become real files in the sandbox for
    # `execute` to run them. Same curl-then-unpack shape as upload_file.

    async def install_skills(self, db: AsyncSession, skill_refs: list[dict[str, Any]]) -> None:
        """Download and unpack every skill referenced by ``skill_refs``.

        ``skill_refs`` is the already-resolved ``EffectiveAgentVersion.skills``
        list (``[{"id": skill_id, "version": "latest" | int}, ...]``) — this
        method does not resolve agent versions itself.
        """
        skills_root = f"{get_settings().vma_e2b_workdir}/skills"
        for ref in skill_refs:
            skill_id = str(ref["id"])
            version_number = ref.get("version", "latest")
            if version_number == "latest":
                skill = await res_q.get_resource(
                    db, resource_id=skill_id, resource_type="skill", organization_id=self._organization_id
                )
                if skill is None:
                    raise ValueError(f"Skill {skill_id} does not exist")
                version_number = skill.data["latest_version"]

            skill_version = await res_q.get_resource_version(
                db,
                resource_type="skill_version",
                parent_id=skill_id,
                version=int(version_number),
                organization_id=self._organization_id,
            )
            if skill_version is None or not skill_version.storage_key:
                raise ValueError(f"Skill {skill_id} version {version_number} is not available")

            # The archive's members are already rooted under top_level_directory/
            # (that's how skills.py validates and builds it — see
            # _validate_skill_files / _zip_uploaded_files) — extract straight
            # into skills_root, not skills_root/top_level_directory, or the
            # directory ends up nested twice.
            top_level_directory = str(skill_version.data["top_level_directory"])
            url = await create_presigned_download_url(skill_version.storage_key)
            zip_path = f"/tmp/{skill_id}-v{version_number}.zip"
            await self.run(f"curl -fsSL -o {shlex.quote(zip_path)} {shlex.quote(url)}", user="root")
            # unzip runs as root, and zips built with Python's zipfile.writestr
            # (no explicit Unix mode) commonly extract as 600 root:root — the
            # guest user couldn't read or execute anything without this chmod.
            extracted_dir = f"{skills_root}/{top_level_directory}"
            await self.run(
                f"unzip -oq {shlex.quote(zip_path)} -d {shlex.quote(skills_root)} "
                f"&& chmod -R a+rX {shlex.quote(extracted_dir)} "
                f"&& rm -f {shlex.quote(zip_path)}",
                user="root",
            )

    # ---- commands -----------------------------------------------------------

    async def run(self, command: str, **kwargs: Any) -> Any:
        await self.ensure_connected()
        kwargs.setdefault("user", self._guest_user)
        kwargs.setdefault("timeout", get_settings().vma_sandbox_command_timeout_seconds)
        return await self._native.commands.run(command, **kwargs)


class LazyE2BBackend(SandboxBackendProtocol):
    """DeepAgents backend that connects on first use and self-heals afterward.

    ``AsyncE2BSandbox`` (langchain_e2b) needs an already-connected native
    sandbox at construction time and never re-validates it — fine for a
    single call, wrong for a whole agent turn where the model can take
    minutes between tool calls and the sandbox can auto-pause mid-turn. This
    wraps it: every call runs ``Sandbox.ensure_connected()`` first, then
    delegates to an ``AsyncE2BSandbox`` rebuilt whenever the underlying
    connection was replaced.
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
                workdir=get_settings().vma_e2b_workdir,
                timeout=self._sandbox._timeout_seconds,
            )
            self._delegate_native = native
        return self._delegate

    @property
    def id(self) -> str:
        return self._sandbox.sandbox_id

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        backend = await self._ready()
        return await backend.aexecute(command, timeout=timeout)

    async def als(self, path: str) -> LsResult:
        backend = await self._ready()
        return await backend.als(path)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        backend = await self._ready()
        return await backend.aread(file_path, offset=offset, limit=limit)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        backend = await self._ready()
        return await backend.awrite(file_path, content)

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
        backend = await self._ready()
        return await backend.aglob(pattern, path=path)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        backend = await self._ready()
        return await backend.agrep(pattern, path=path, glob=glob)


__all__ = ["LazyE2BBackend", "Sandbox"]
