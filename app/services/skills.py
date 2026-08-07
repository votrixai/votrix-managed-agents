"""Skill use cases.

A skill is a zip that gets unpacked into a sandbox before a run. The bytes go
to object storage; the row only remembers where they went.

The package is inspected on the way in — its own SKILL.md is what names it, so
a skill cannot claim to be something it is not.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Skill
from app.db.queries import DEFAULT_PAGE_SIZE, Page
from app.db.queries import skills as skills_q
from app.models.errors import Conflict, NotFound
from app.utils import storage

# A skill package is a handful of markdown and scripts. Anything wildly beyond
# that is a mistake or an attack, and either way should not reach storage.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_MEMBERS = 1_000
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1_000

# From the Agent Skills specification, and enforced again by DeepAgents when it
# loads a skill — so a package that breaks these would store fine here and then
# never show up in the sandbox.
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1_024
NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class InvalidSkillPackage(ValueError):
    """The upload is not a usable skill package."""


@dataclass(frozen=True)
class SkillManifest:
    """What a package says about itself, once it has been checked."""

    name: str
    description: str
    # True when the zip already contains a `<name>/` directory, which decides
    # whether unpacking needs one created around it.
    has_own_directory: bool


async def create_skill(
    db: AsyncSession,
    *,
    organization_id: str,
    content: bytes,
    description: str | None = None,
) -> Skill:
    """Store an uploaded package, after checking it is one.

    The package names itself and the caller cannot override that: the Agent
    Skills spec ties the name to the `name:` in SKILL.md and to the directory
    around it, so a name supplied from outside would contradict the file we
    just stored and the skill would never load.
    """
    manifest = inspect_package(content)
    resolved_name = manifest.name

    existing = await skills_q.get_skill_by_name(
        db, name=resolved_name, organization_id=organization_id
    )
    if existing is not None:
        raise Conflict(f"A skill named {resolved_name!r} already exists")

    stored = await storage.save_bytes(
        normalize_package(content, name=resolved_name),
        organization_id=organization_id,
        category="skills",
        filename=f"{resolved_name}.zip",
        mime_type="application/zip",
    )
    skill = await skills_q.create_skill(
        db,
        organization_id=organization_id,
        name=resolved_name,
        description=description or manifest.description,
        storage_key=stored.key,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
    )
    await db.commit()
    return skill


async def get_skill(db: AsyncSession, *, skill_id: str, organization_id: str) -> Skill:
    skill = await skills_q.get_skill(db, skill_id=skill_id, organization_id=organization_id)
    if skill is None:
        raise NotFound(f"Skill {skill_id} not found")
    return skill


async def list_skills(
    db: AsyncSession,
    *,
    organization_id: str,
    include_archived: bool = False,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: str | None = None,
    after_id: str | None = None,
) -> Page:
    return await skills_q.list_skills(
        db,
        organization_id=organization_id,
        include_archived=include_archived,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
    )


async def update_skill(
    db: AsyncSession,
    *,
    skill_id: str,
    organization_id: str,
    content: bytes | None = None,
    name: str | None = None,
    description: str | None = None,
) -> Skill:
    """Edit a skill, replacing its package when one is supplied.

    The id survives a replacement, which is the whole point: an Agent names a
    skill by id, so changing what a skill contains must not mean re-pointing
    every Agent that uses it.

    A new package names the skill, exactly as on create — the Agent Skills spec
    ties the name to SKILL.md, so a name from the form would contradict the file
    just stored and the skill would silently fail to load. Renaming this way is
    allowed; taking a name another skill already holds is not.
    """
    skill = await get_skill(db, skill_id=skill_id, organization_id=organization_id)

    if content is None:
        await skills_q.update_skill(db, skill, name=name, description=description)
        await db.commit()
        return skill

    manifest = inspect_package(content)
    resolved_name = manifest.name
    if resolved_name != skill.name:
        existing = await skills_q.get_skill_by_name(
            db, name=resolved_name, organization_id=organization_id
        )
        if existing is not None and existing.id != skill.id:
            raise Conflict(f"A skill named {resolved_name!r} already exists")

    stored = await storage.save_bytes(
        normalize_package(content, name=resolved_name),
        organization_id=organization_id,
        category="skills",
        filename=f"{resolved_name}.zip",
        mime_type="application/zip",
    )
    await skills_q.update_skill(
        db,
        skill,
        name=resolved_name,
        description=description or manifest.description,
        storage_key=stored.key,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
    )
    await db.commit()
    # The object the skill used to point at is left where it is. A key carries
    # its content's digest, so the new package never lands on top of the old one
    # and nothing has to be deleted for this to be correct — reclaiming what is
    # now unreferenced is a sweep, not part of an update.
    return skill


async def download_skill(
    db: AsyncSession,
    *,
    skill_id: str,
    organization_id: str,
) -> tuple[bytes, str]:
    """Fetch the package by id. The storage key stays on this side."""
    skill = await get_skill(db, skill_id=skill_id, organization_id=organization_id)
    content, content_type = await storage.download_bytes(skill.storage_key)
    return content, content_type or "application/zip"


async def delete_skill(db: AsyncSession, *, skill_id: str, organization_id: str) -> Skill:
    skill = await get_skill(db, skill_id=skill_id, organization_id=organization_id)
    await storage.delete_object(skill.storage_key)
    await skills_q.delete_skill(db, skill)
    await db.commit()
    return skill


# --- the package itself ------------------------------------------------------


def inspect_package(content: bytes) -> SkillManifest:
    """Validate the zip and read what it says about itself.

    Runs before anything is stored. A package is about to be unpacked inside a
    sandbox and read by the agent as instructions, which is exactly the wrong
    thing to take on trust.

    The name is checked rather than cleaned up. DeepAgents refuses to load a
    skill whose `name` does not equal its directory name, so a name we quietly
    rewrote would store fine and then silently fail to appear in the sandbox —
    much worse than a rejected upload.
    """
    if len(content) > MAX_UPLOAD_BYTES:
        raise InvalidSkillPackage("The package is too large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise InvalidSkillPackage("Not a zip file") from exc

    members = archive.infolist()
    if len(members) > MAX_MEMBERS:
        raise InvalidSkillPackage(f"More than {MAX_MEMBERS} entries")

    unpacked = 0
    for member in members:
        _check_member(member)
        unpacked += member.file_size
        if unpacked > MAX_UNPACKED_BYTES:
            raise InvalidSkillPackage("Unpacks to more than the size limit")

    manifest_path = _find_manifest(members)
    fields = _frontmatter(archive.read(manifest_path))

    name = str(fields.get("name") or "").strip()
    _check_name(name)

    description = str(fields.get("description") or "").strip()
    if not description:
        raise InvalidSkillPackage("SKILL.md must declare a description")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise InvalidSkillPackage(f"description exceeds {MAX_DESCRIPTION_LENGTH} characters")

    # `pptx/SKILL.md` means the zip carries its own directory, so unpacking it
    # into `skills/` already produces `skills/pptx/`. `SKILL.md` at the root
    # needs that directory created around it.
    prefix = manifest_path.rsplit("/", 1)[0] if "/" in manifest_path else ""
    if prefix and prefix != name:
        raise InvalidSkillPackage(
            f"the directory holding SKILL.md is {prefix!r} but the skill is named {name!r}"
        )
    return SkillManifest(name=name, description=description, has_own_directory=bool(prefix))


def _find_manifest(members: list[zipfile.ZipInfo]) -> str:
    """Locate SKILL.md, which must be at the root or one directory down."""
    candidates = [m.filename for m in members if m.filename.rsplit("/", 1)[-1] == "SKILL.md"]
    if not candidates:
        raise InvalidSkillPackage("The package has no SKILL.md")
    shallow = [c for c in candidates if c.count("/") <= 1]
    if not shallow:
        raise InvalidSkillPackage("SKILL.md must be at the top of the package")
    if len(shallow) > 1:
        raise InvalidSkillPackage("The package contains more than one SKILL.md")
    return shallow[0]


def _check_name(name: str) -> None:
    """Enforce the Agent Skills naming rules.

    Lowercase letters, digits and single inner hyphens — the same rules
    DeepAgents applies when it loads the skill, checked here so a package that
    would not load never gets stored.
    """
    if not name:
        raise InvalidSkillPackage("SKILL.md must declare a name")
    if len(name) > MAX_NAME_LENGTH:
        raise InvalidSkillPackage(f"name exceeds {MAX_NAME_LENGTH} characters")
    if not NAME_PATTERN.fullmatch(name):
        raise InvalidSkillPackage(
            f"name {name!r} must be lowercase letters, digits and single hyphens, "
            "and may not start or end with one"
        )


def _check_member(member: zipfile.ZipInfo) -> None:
    name = member.filename
    if name.startswith("/") or ".." in name.split("/"):
        # Unpacking this would write outside the directory it was given.
        raise InvalidSkillPackage(f"Unsafe path in package: {name}")
    if (member.external_attr >> 16) & 0o170000 == 0o120000:
        raise InvalidSkillPackage(f"Symlinks are not allowed: {name}")
    if member.compress_size and member.file_size / member.compress_size > MAX_COMPRESSION_RATIO:
        # A few kilobytes that expand to gigabytes — a zip bomb.
        raise InvalidSkillPackage(f"Suspicious compression ratio: {name}")


def _frontmatter(raw: bytes) -> dict[str, Any]:
    """Parse the YAML block fenced by `---` at the top of SKILL.md."""
    text = raw.decode("utf-8", errors="replace").lstrip("\ufeff")
    if not text.startswith("---"):
        raise InvalidSkillPackage("SKILL.md must begin with a YAML frontmatter block")
    block, sep, _ = text[3:].partition("\n---")
    if not sep:
        raise InvalidSkillPackage("The frontmatter block in SKILL.md is not closed")
    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        raise InvalidSkillPackage(f"The frontmatter in SKILL.md is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise InvalidSkillPackage("The frontmatter in SKILL.md must be a mapping")
    return parsed


def normalize_package(content: bytes, *, name: str) -> bytes:
    """Rewrite the package so it always carries its own `<name>/` directory.

    Uploads arrive both ways — SKILL.md at the root, or already inside a
    directory. Storing one canonical shape means the sandbox has a single
    unpacking rule instead of a flag it has to remember per skill.
    """
    source = zipfile.ZipFile(io.BytesIO(content))
    if all(member.filename.startswith(f"{name}/") for member in source.infolist()):
        return content

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for member in source.infolist():
            if member.is_dir():
                continue
            target.writestr(f"{name}/{member.filename}", source.read(member))
    return buffer.getvalue()


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
