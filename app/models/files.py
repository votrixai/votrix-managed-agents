from datetime import datetime
from typing import Literal

from pydantic import Field

from app.models.common import ApiModel


class FileScope(ApiModel):
    """What a file came out of.

    An object rather than a bare id so the kind of thing is stated rather than
    inferred from a prefix. A file comes out of a Session when an agent leaves
    it in `outputs/`, and out of a Sandbox when its holder asks for one by
    path; both are ordinary files wearing a label saying where they came from.
    """

    type: Literal["session", "sandbox"] = "session"
    id: str

    @classmethod
    def for_id(cls, scope_id: str) -> "FileScope":
        """Label a scope id with the kind of thing it names.

        Read off the id's prefix, which is the one place the kind is already
        recorded. A column beside it would be the better answer if anything
        ever needed to query by kind; nothing does, and a second copy of a fact
        is a second thing to keep true.
        """
        return cls(type="sandbox" if scope_id.startswith("sbx_") else "session", id=scope_id)


class FileResponse(ApiModel):
    id: str
    type: Literal["file"] = "file"
    filename: str
    mime_type: str | None = None
    size_bytes: int
    sha256: str | None = None
    # Absent for anything a user uploaded. An output file is an ordinary file
    # wearing a label saying which run it came out of, not a different kind.
    scope: FileScope | None = None
    # True once the path this file was at no longer exists in its session's
    # sandbox. An archived file is absent from every listing and its bytes are
    # still downloadable, which is what lets an id that was quoted somewhere
    # keep resolving after the agent deleted the file.
    archived: bool = False
    created_at: datetime
    updated_at: datetime


class LiveFileRequest(ApiModel):
    """Take one file out of a running session's sandbox, now.

    `path` is relative to the sandbox's `outputs/` directory. A session's
    outputs are collected when its turn ends; this is for the case where the
    agent has finished a deliverable and the user wants it before the rest of
    the turn is.
    """

    path: str = Field(min_length=1, max_length=512)


class LiveUploadRequest(ApiModel):
    """Put one durable VMA File into an existing Session sandbox.

    The file must already exist under ``/v1/files``. ``path`` is relative to
    the sandbox's ``uploads/`` directory and defaults to the File's filename,
    matching the file-resource contract used when a Session is created.
    """

    file_id: str = Field(min_length=1, max_length=64)
    path: str | None = Field(default=None, max_length=512)
