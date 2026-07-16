"""Persist files discovered in a managed session's sandbox output directory.

Discovery is deliberately provider-specific and lives outside this module.  This
component accepts already-read regular files, validates the trust-boundary
metadata supplied by discovery, and snapshots new output versions into object
storage plus the existing ``file`` resource table.

The database transaction is owned by the caller.  Object storage writes happen
before the corresponding resource is flushed, so callers should invoke this at
a lifecycle boundary where a retry is safe.  An exact ``(session, path, sha256)``
retry is idempotent; new bytes at the same path create a new, still-addressable
File resource.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.db.models import ManagedResource, ManagedSession
from app.db.queries import resources as res_q
from app.db.queries import sessions as sessions_q
from app.runtime.sandbox_inputs import SESSION_OUTPUT_ROOT


SANDBOX_OUTPUT_ROOT = SESSION_OUTPUT_ROOT
MAX_DISCOVERED_OUTPUT_FILES = 100
# Discovery currently crosses the E2B command channel as base64 JSON. Keep the
# aggregate deliberately bounded to avoid large RPC/stdout and transient-memory
# amplification until a streaming provider file-transfer path is implemented.
MAX_OUTPUT_FILE_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_TOTAL_BYTES = 100 * 1024 * 1024
MAX_OUTPUT_FILENAME_BYTES = 255


class SandboxOutputValidationError(ValueError):
    """A discovered entry cannot be safely exported as a session File."""


@dataclass(frozen=True, slots=True)
class DiscoveredSandboxOutput:
    """One file and its provider-attested filesystem metadata."""

    path: str
    content: bytes
    mime_type: str | None = None
    is_regular_file: bool = True
    is_symlink: bool = False
    hardlink_count: int = 1


@dataclass(frozen=True, slots=True)
class _ValidatedOutput:
    path: str
    filename: str
    content: bytes
    mime_type: str | None
    sha256: str


async def persist_discovered_outputs(
    db: AsyncSession,
    session: ManagedSession,
    entries: Sequence[DiscoveredSandboxOutput],
) -> list[ManagedResource]:
    """Snapshot newly discovered direct output files for ``session``.

    Only direct children of :data:`SANDBOX_OUTPUT_ROOT` are accepted.  The
    complete batch is validated before any object is written.  The session row
    is then locked so concurrent discovery attempts cannot both create the same
    path/content version.

    The returned list contains only newly created File resources.  The caller
    decides when to commit the surrounding database transaction.
    """

    validated = _validate_batch(entries)
    if not validated:
        return []
    if session.deleted_at is not None:
        raise SandboxOutputValidationError("Cannot persist outputs for a deleted Session")

    locked_session = await sessions_q.get_session(
        db,
        session.id,
        organization_id=session.organization_id,
        for_update=True,
    )
    if locked_session is None or locked_session.deleted_at is not None:
        raise SandboxOutputValidationError("Session does not exist or has been deleted")

    existing = await _existing_output_identities(
        db,
        organization_id=locked_session.organization_id,
        session_id=locked_session.id,
        sha256_values={item.sha256 for item in validated},
    )

    created: list[ManagedResource] = []
    for item in validated:
        identity = (item.path, item.sha256)
        if identity in existing:
            continue

        stored = await storage.save_file_bytes(
            item.content,
            item.mime_type,
            namespace=f"sessions_{locked_session.id}",
            category="outputs",
            filename=item.filename,
            organization_id=locked_session.organization_id,
        )
        resource = await res_q.create_resource(
            db,
            resource_type="file",
            parent_id=locked_session.id,
            name=item.path,
            filename=item.filename,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            storage_backend=stored.backend,
            storage_key=stored.key,
            storage_url=None,
            organization_id=locked_session.organization_id,
            data={
                "filename": item.filename,
                "mime_type": stored.content_type,
                "downloadable": True,
                "sandbox_path": item.path,
                "scope": {"type": "session", "id": locked_session.id},
                "source": "sandbox_output",
                "sha256": stored.sha256,
            },
        )
        created.append(resource)
        existing.add(identity)

    return created


def _validate_batch(entries: Sequence[DiscoveredSandboxOutput]) -> list[_ValidatedOutput]:
    if isinstance(entries, (str, bytes, bytearray)):
        raise SandboxOutputValidationError("Output entries must be a sequence of discovered files")
    try:
        materialized = list(entries)
    except TypeError as exc:
        raise SandboxOutputValidationError("Output entries must be a sequence of discovered files") from exc

    if len(materialized) > MAX_DISCOVERED_OUTPUT_FILES:
        raise SandboxOutputValidationError(
            f"At most {MAX_DISCOVERED_OUTPUT_FILES} sandbox output files may be discovered at once"
        )

    total_bytes = 0
    by_path: dict[str, _ValidatedOutput] = {}
    for entry in materialized:
        if not isinstance(entry, DiscoveredSandboxOutput):
            raise SandboxOutputValidationError("Every output entry must be DiscoveredSandboxOutput")
        validated = _validate_entry(entry)
        total_bytes += len(validated.content)
        if total_bytes > MAX_OUTPUT_TOTAL_BYTES:
            raise SandboxOutputValidationError(
                f"Sandbox output batch exceeds {MAX_OUTPUT_TOTAL_BYTES} bytes"
            )

        previous = by_path.get(validated.path)
        if previous is not None:
            if previous.sha256 != validated.sha256:
                raise SandboxOutputValidationError(
                    f"Discovery returned conflicting contents for {validated.path}"
                )
            continue
        by_path[validated.path] = validated

    return list(by_path.values())


def _validate_entry(entry: DiscoveredSandboxOutput) -> _ValidatedOutput:
    if not isinstance(entry.path, str):
        raise SandboxOutputValidationError("Sandbox output path must be a string")
    if not entry.path or any(ord(character) < 32 for character in entry.path):
        raise SandboxOutputValidationError("Sandbox output path contains invalid characters")

    path = PurePosixPath(entry.path)
    if (
        not entry.path.startswith("/")
        or entry.path != str(path)
        or path.parent != PurePosixPath(SANDBOX_OUTPUT_ROOT)
        or path.name in {"", ".", ".."}
    ):
        raise SandboxOutputValidationError(
            f"Sandbox outputs must be direct files under {SANDBOX_OUTPUT_ROOT}"
        )
    if len(path.name.encode("utf-8")) > MAX_OUTPUT_FILENAME_BYTES:
        raise SandboxOutputValidationError(
            f"Sandbox output filename exceeds {MAX_OUTPUT_FILENAME_BYTES} bytes"
        )
    if entry.is_symlink:
        raise SandboxOutputValidationError("Sandbox output symlinks are not exportable")
    if not entry.is_regular_file:
        raise SandboxOutputValidationError("Sandbox output must be a regular file")
    if (
        isinstance(entry.hardlink_count, bool)
        or not isinstance(entry.hardlink_count, int)
        or entry.hardlink_count != 1
    ):
        raise SandboxOutputValidationError("Sandbox output hardlinks are not exportable")
    if not isinstance(entry.content, bytes):
        raise SandboxOutputValidationError("Sandbox output content must be bytes")
    if len(entry.content) > MAX_OUTPUT_FILE_BYTES:
        raise SandboxOutputValidationError(
            f"Sandbox output file exceeds {MAX_OUTPUT_FILE_BYTES} bytes"
        )

    mime_type = _validated_mime_type(entry.mime_type)
    return _ValidatedOutput(
        path=entry.path,
        filename=path.name,
        content=entry.content,
        mime_type=mime_type,
        sha256=hashlib.sha256(entry.content).hexdigest(),
    )


def _validated_mime_type(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > 255
        or "/" not in value
        or any(ord(character) < 32 for character in value)
    ):
        raise SandboxOutputValidationError("Sandbox output MIME type is invalid")
    return value


async def _existing_output_identities(
    db: AsyncSession,
    *,
    organization_id: str,
    session_id: str,
    sha256_values: set[str],
) -> set[tuple[str, str]]:
    if not sha256_values:
        return set()
    result = await db.execute(
        select(ManagedResource).where(
            ManagedResource.organization_id == organization_id,
            ManagedResource.resource_type == "file",
            ManagedResource.parent_id == session_id,
            ManagedResource.sha256.in_(sha256_values),
            ManagedResource.deleted_at.is_(None),
        )
    )
    identities: set[tuple[str, str]] = set()
    for resource in result.scalars():
        data = resource.data or {}
        path = data.get("sandbox_path")
        if data.get("source") == "sandbox_output" and isinstance(path, str) and resource.sha256:
            identities.add((path, resource.sha256))
    return identities


__all__ = [
    "DiscoveredSandboxOutput",
    "MAX_DISCOVERED_OUTPUT_FILES",
    "MAX_OUTPUT_FILE_BYTES",
    "MAX_OUTPUT_TOTAL_BYTES",
    "SANDBOX_OUTPUT_ROOT",
    "SandboxOutputValidationError",
    "persist_discovered_outputs",
]
