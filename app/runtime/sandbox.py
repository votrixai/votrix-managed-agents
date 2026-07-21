"""One class, one file: everything about an E2B sandbox for a VMA session."""

from __future__ import annotations

import mimetypes
import shlex
from typing import Any

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
    """One instance = one connected E2B sandbox, bound to one VMA session."""

    def __init__(
        self,
        native: AsyncSandbox,
        session_id: str,
        organization_id: str,
        guest_user: str,
        timeout_seconds: int,
    ) -> None:
        self._native = native
        self._session_id = session_id
        self._organization_id = organization_id
        self._guest_user = guest_user
        self._timeout_seconds = timeout_seconds

    @property
    def sandbox_id(self) -> str:
        return self._native.sandbox_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def organization_id(self) -> str:
        return self._organization_id

    @property
    def to_async_e2b_sandbox(self) -> AsyncE2BSandbox:
        """Wrap this sandbox so it can be passed as create_deep_agent(backend=...)."""
        return AsyncE2BSandbox(
            sandbox=self._native,
            workdir=get_settings().vma_e2b_workdir,
            timeout=self._timeout_seconds,
        )

    # ---- construction: knows about VMA sessions/db ----------------------

    @classmethod
    async def provision(cls, db: AsyncSession, session_id: str, organization_id: str, template: str) -> Sandbox:
        """Create a brand-new E2B sandbox and persist its id for this session.

        Called once, the first time a session needs a sandbox. ``template`` is
        an E2B template name (or ``name:tag``) — callers choose it, since it
        is a per-session decision, not a global setting.
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
        return cls(
            native,
            session_id,
            organization_id,
            settings.vma_e2b_guest_user,
            settings.vma_e2b_timeout_seconds,
        )

    @classmethod
    async def connect(cls, sandbox_id: str, session_id: str, organization_id: str) -> Sandbox:
        """Reconnect to an already-known E2B sandbox id.

        The caller looks up ``sandbox_id`` (e.g. via
        ``get_session_sandbox().external_sandbox_id``) — this classmethod does
        not touch the database. Called once per turn.
        """
        settings = get_settings()
        native = await AsyncSandbox.connect(
            sandbox_id,
            timeout=settings.vma_e2b_timeout_seconds,
            api_key=settings.e2b_api_key,
        )
        return cls(
            native,
            session_id,
            organization_id,
            settings.vma_e2b_guest_user,
            settings.vma_e2b_timeout_seconds,
        )

    # ---- lifecycle --------------------------------------------------------

    async def pause(self) -> None:
        await self._native.pause(keep_memory=get_settings().vma_e2b_keep_memory)

    async def kill(self) -> None:
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

    # ---- memory / skills ------------------------------------------------------
    # Memory content already lives in the database as plain text — no R2, no
    # curl, just one batched write straight to e2b. Skills are zip archives in
    # R2, so they follow the same curl-then-unpack shape as upload_file.

    async def install_memory(self, db: AsyncSession) -> None:
        """Write every memory file attached to this session into the sandbox.

        One batched ``write_files`` call regardless of how many memory files
        exist, so this doesn't add one network round trip per file.
        """
        session_resources = await res_q.list_resources(
            db,
            resource_type="session_resource",
            parent_id=self._session_id,
            organization_id=self._organization_id,
            limit=1000,
        )
        entries: list[dict[str, Any]] = []
        for resource in session_resources:
            data = resource.data or {}
            if data.get("type") != "memory_store":
                continue
            mount_path = str(data["mount_path"])
            memories = await res_q.list_resources(
                db,
                resource_type="memory",
                parent_id=str(data["memory_store_id"]),
                organization_id=self._organization_id,
                limit=1000,
            )
            for memory in memories:
                memory_data = memory.data or {}
                entries.append(
                    {
                        "path": f"{mount_path}/{memory_data['path_key']}",
                        "data": str(memory_data.get("content") or ""),
                    }
                )

        if entries:
            await self._native.files.write_files(entries, user=self._guest_user)

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

            top_level_directory = str(skill_version.data["top_level_directory"])
            url = await create_presigned_download_url(skill_version.storage_key)
            zip_path = f"/tmp/{skill_id}-v{version_number}.zip"
            await self.run(f"curl -fsSL -o {shlex.quote(zip_path)} {shlex.quote(url)}", user="root")
            await self.run(
                f"unzip -oq {shlex.quote(zip_path)} -d {shlex.quote(f'{skills_root}/{top_level_directory}')} "
                f"&& rm -f {shlex.quote(zip_path)}",
                user="root",
            )

    # ---- commands -----------------------------------------------------------

    async def run(self, command: str, **kwargs: Any) -> Any:
        await self._native.set_timeout(self._timeout_seconds)
        kwargs.setdefault("user", self._guest_user)
        kwargs.setdefault("timeout", get_settings().vma_sandbox_command_timeout_seconds)
        return await self._native.commands.run(command, **kwargs)


__all__ = ["Sandbox"]
