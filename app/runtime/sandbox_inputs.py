"""Deterministic, session-initial sandbox inputs.

The control plane materializes this bundle exactly once when an E2B-backed
session is created. Later turns recompute its create-time identity, verify the
immutable subset, and do not upload the files again.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


class SandboxInputError(RuntimeError):
    """Raised when managed inputs cannot be represented safely."""


@dataclass(frozen=True, slots=True)
class SandboxInputFile:
    path: str
    content: bytes
    read_only: bool
    source: str


@dataclass(frozen=True, slots=True)
class SandboxInputBundle:
    files: tuple[SandboxInputFile, ...]
    skill_sources: tuple[str, ...]
    memory_sources: tuple[str, ...]
    mutable_roots: tuple[str, ...]

    @property
    def immutable_files(self) -> tuple[SandboxInputFile, ...]:
        return tuple(item for item in self.files if item.read_only)

    @property
    def input_digest(self) -> str:
        """Identify every create-time input without tracking later sandbox edits.

        Read-write memory seeds participate in this identity even though their
        sandbox copies remain mutable.  The digest is recomputed from the
        control-plane source, so an Agent editing its sandbox memory does not
        change it, while changing the source seed makes resume fail closed.
        """
        descriptor = {
            "version": "vma-session-inputs-v1",
            "files": [
                {
                    "path": item.path,
                    "sha256": hashlib.sha256(item.content).hexdigest(),
                    "read_only": item.read_only,
                    "source": item.source,
                }
                for item in self.files
            ],
            "skill_sources": list(self.skill_sources),
            "memory_sources": list(self.memory_sources),
            "mutable_roots": list(self.mutable_roots),
        }
        encoded = json.dumps(
            descriptor,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    @property
    def immutable_manifest(self) -> dict[str, str]:
        return {
            item.path: hashlib.sha256(item.content).hexdigest()
            for item in self.immutable_files
        }

    def upload_pairs(self) -> list[tuple[str, bytes]]:
        return [(item.path, item.content) for item in self.files]

    def state_files(self) -> dict[str, dict[str, Any]]:
        return {item.path: _file_data(item.content) for item in self.files}


def sandbox_input_bundle(
    runtime_context: dict[str, Any],
    *,
    reject_unsupported_resources: bool = False,
) -> SandboxInputBundle:
    """Build canonical paths and bytes from one session's pinned context."""
    unsupported = sorted(
        {
            str(item)
            for item in runtime_context.get("session_resource_types") or []
            if str(item) not in {"file", "memory_store"}
        }
    )
    if reject_unsupported_resources and unsupported:
        raise SandboxInputError(
            "Sandbox materialization is not implemented for Session resource types: "
            + ", ".join(unsupported)
        )

    by_path: dict[str, SandboxInputFile] = {}
    skill_sources: list[str] = []
    memory_sources: list[str] = []
    mutable_roots: set[str] = set()

    for item in runtime_context.get("session_files") or []:
        if not isinstance(item, dict) or not isinstance(item.get("content"), bytes):
            continue
        path = _safe_virtual_path(str(item.get("path") or ""))
        _add_file(
            by_path,
            SandboxInputFile(
                path=path,
                content=item["content"],
                read_only=bool(item.get("read_only", True)),
                source="session_file",
            ),
        )

    for archive in runtime_context.get("skill_archives") or []:
        if not isinstance(archive, dict) or not isinstance(archive.get("archive"), bytes):
            continue
        skill_id = str(archive.get("skill_id") or "skill")
        version = str(archive.get("version") or "unknown")
        identity = hashlib.sha256(skill_id.encode("utf-8")).hexdigest()[:10]
        root = f"/skills/custom/{_safe_segment(skill_id)}-{identity}/v{_safe_segment(version)}"
        extracted = _extract_skill_archive(archive["archive"])
        for relative_path, content in extracted:
            _add_file(
                by_path,
                SandboxInputFile(
                    path=f"{root}/{relative_path}",
                    content=content,
                    read_only=True,
                    source="skill",
                ),
            )
        if extracted:
            skill_sources.append(root + "/")

    for store in runtime_context.get("memory_stores") or []:
        if not isinstance(store, dict):
            continue
        mount = _safe_virtual_path(
            str(store.get("mount_path") or f"/mnt/memory/{store.get('memory_store_id')}")
        )
        read_only = str(store.get("access") or "read_write") == "read_only"
        if not read_only:
            mutable_roots.add(mount)
        memory_lines: list[str] = []
        if store.get("instructions"):
            memory_lines.append(str(store["instructions"]))
        for memory in store.get("memories") or []:
            if not isinstance(memory, dict):
                continue
            relative = str(
                memory.get("path")
                or memory.get("path_key")
                or memory.get("memory_id")
                or "memory.md"
            ).lstrip("/")
            path = _safe_virtual_path(f"{mount}/{relative}")
            _add_file(
                by_path,
                SandboxInputFile(
                    path=path,
                    content=str(memory.get("content") or "").encode(),
                    read_only=read_only,
                    source="memory_read_only" if read_only else "memory_seed",
                ),
            )
            memory_lines.append(f"- {path}")
        agents_path = _safe_virtual_path(f"{mount}/AGENTS.md")
        _add_file(
            by_path,
            SandboxInputFile(
                path=agents_path,
                content="\n".join(memory_lines).encode(),
                read_only=read_only,
                source="memory_read_only" if read_only else "memory_seed",
            ),
        )
        memory_sources.append(agents_path)

    return SandboxInputBundle(
        files=tuple(by_path[path] for path in sorted(by_path)),
        skill_sources=tuple(sorted(set(skill_sources))),
        memory_sources=tuple(sorted(set(memory_sources))),
        mutable_roots=tuple(sorted(mutable_roots)),
    )


def _add_file(target: dict[str, SandboxInputFile], item: SandboxInputFile) -> None:
    existing = target.get(item.path)
    if existing is not None and existing != item:
        raise SandboxInputError(f"Managed sandbox inputs collide at {item.path}")
    for path in target:
        if path.startswith(item.path + "/") or item.path.startswith(path + "/"):
            raise SandboxInputError(
                f"Managed sandbox input is both a file and directory: {item.path}"
            )
    target[item.path] = item


def _extract_skill_archive(content: bytes) -> list[tuple[str, bytes]]:
    extracted: list[tuple[str, bytes]] = []
    total = 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise SandboxInputError("Skill archive is not a valid ZIP file") from exc
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > 512:
            raise SandboxInputError("Skill archive contains too many files")
        for info in infos:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise SandboxInputError("Skill archive contains an unsafe path")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise SandboxInputError("Skill archive symlinks are not supported")
            total += int(info.file_size)
            if total > 100 * 1024 * 1024:
                raise SandboxInputError("Skill archive expands beyond 100 MB")
            extracted.append((str(path), archive.read(info)))
    return extracted


def _safe_virtual_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value.startswith("/") or ".." in path.parts or value != str(path):
        raise SandboxInputError(f"Unsafe virtual path: {value!r}")
    return str(path)


def _safe_segment(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-").lower()
    if normalized in {"", ".", ".."}:
        return "item"
    return normalized[:80]


def _file_data(content: bytes) -> dict[str, Any]:
    try:
        return {"content": content.decode("utf-8"), "encoding": "utf-8"}
    except UnicodeDecodeError:
        return {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        }


__all__ = [
    "SandboxInputBundle",
    "SandboxInputError",
    "SandboxInputFile",
    "sandbox_input_bundle",
]
