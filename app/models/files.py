from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.models.common import FlexibleApiModel


class FileScopeResponse(FlexibleApiModel):
    type: Literal["session"] = Field(description="Scope discriminator for a Session-owned file.")
    id: str = Field(description="Identifier of the Session that owns this file copy.")


class FileResponse(FlexibleApiModel):
    """Public metadata for a file; object-store coordinates are never exposed."""

    id: str
    type: Literal["file"] = "file"
    name: str | None = None
    filename: str | None = Field(default=None, description="Original or generated filename.")
    mime_type: str | None = Field(default=None, description="Media type recorded for the file content.")
    size_bytes: int | None = Field(default=None, description="Stored file size in bytes.")
    sha256: str | None = Field(default=None, description="Lowercase hexadecimal SHA-256 digest of the file content.")
    deduplicated_from_file_id: str | None = Field(
        default=None,
        description="Identifier of an existing file whose immutable object is shared by this record.",
    )
    scope: FileScopeResponse | None = Field(
        default=None,
        description="Session ownership details when this is an isolated Session file copy.",
    )
    status: str | None = Field(default=None, description="Current lifecycle status when one is assigned.")
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    deleted_at: datetime | None = None


class FileDeletedResponse(FlexibleApiModel):
    id: str
    type: Literal["file_deleted"] = "file_deleted"
    deleted: Literal[True] = True


class PresignedFileUploadResponse(FlexibleApiModel):
    type: Literal["file_upload_url"] = "file_upload_url"
    key: str = Field(description="Workspace-scoped staging object key to pass to the completion request.")
    upload_url: str = Field(description="Time-limited URL that accepts the file bytes.")
    method: Literal["PUT"] = Field(default="PUT", description="HTTP method required by the upload URL.")
    headers: dict[str, str] = Field(description="Headers that must be sent with the upload request.")
    expires_in: int = Field(description="Number of seconds before the upload URL expires.")
