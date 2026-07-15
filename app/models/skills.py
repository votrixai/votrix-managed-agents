from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.models.common import FlexibleApiModel


class SkillFileResponse(FlexibleApiModel):
    filename: str = Field(description="Path of the file within the skill directory.")
    mime_type: str | None = Field(default=None, description="Media type supplied for the skill file.")
    size_bytes: int = Field(description="Uncompressed size of the skill file in bytes.")


class SkillVersionResponse(FlexibleApiModel):
    id: str
    type: Literal["skill_version"] = "skill_version"
    skill_id: str = Field(description="Identifier of the parent skill.")
    version: str
    name: str
    description: str
    directory: str = Field(description="Top-level directory that contains the skill package.")
    top_level_directory: str | None = Field(
        default=None,
        description="Original top-level directory detected in the uploaded package.",
    )
    files: list[SkillFileResponse] = Field(
        default_factory=list,
        description="Files included in this immutable skill version.",
    )
    manifest: dict[str, Any] | None = Field(
        default=None,
        description="Validated metadata parsed from the root SKILL.md file.",
    )
    archive_format: str | None = Field(default=None, description="Archive format used for the stored package.")
    filename: str | None = Field(default=None, description="Filename used when downloading this version.")
    mime_type: str | None = Field(default=None, description="Media type of the stored skill archive.")
    size_bytes: int | None = Field(default=None, description="Stored archive size in bytes.")
    sha256: str | None = Field(default=None, description="Lowercase hexadecimal SHA-256 digest of the archive.")
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    deleted_at: datetime | None = None


class SkillResponse(FlexibleApiModel):
    id: str
    type: Literal["skill"] = "skill"
    name: str | None = None
    display_title: str | None = Field(default=None, description="Optional human-readable title for the skill.")
    description: str | None = None
    top_level_directory: str | None = Field(
        default=None,
        description="Top-level directory detected in the latest uploaded package.",
    )
    latest_version: str | None = Field(default=None, description="Identifier of the latest immutable skill version.")
    source: Literal["anthropic", "custom"] = Field(description="Origin of this skill package.")
    version: SkillVersionResponse | None = Field(
        default=None,
        description="New immutable version returned inline when a skill is created.",
    )
    status: str | None = Field(default=None, description="Current lifecycle status when one is assigned.")
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    deleted_at: datetime | None = None


class SkillDeletedResponse(FlexibleApiModel):
    id: str
    type: Literal["skill_deleted"] = "skill_deleted"
    deleted: Literal[True] = True


class SkillVersionDeletedResponse(FlexibleApiModel):
    id: str = Field(description="String form of the deleted skill version number.")
    type: Literal["skill_version_deleted"] = "skill_version_deleted"
    deleted: Literal[True] = True
