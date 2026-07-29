from datetime import datetime
from typing import Literal

from pydantic import Field

from app.models.common import ApiModel


class FileScope(ApiModel):
    """What a file came out of.

    An object rather than a bare id so the kind of thing is stated rather than
    inferred from a prefix — today only a session produces files, and that will
    not always be true.
    """

    type: Literal["session"] = "session"
    id: str


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
