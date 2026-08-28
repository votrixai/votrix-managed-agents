from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, TimestampMixin


class File(TimestampMixin, Base):
    """A file whose bytes live in object storage.

    A row exists only once its bytes do, so there is no half-uploaded state to
    represent: every row here is a file that can be downloaded.

    `storage_key` is internal: downloads are served by authenticated routes,
    never by handing the bucket path to a caller.
    """

    __tablename__ = "files"
    __table_args__ = (
        # How a session's outputs are read back, which is the one query that
        # has to stay fast however many files an organization accumulates.
        Index("ix_files_scope", "organization_id", "scope_id"),
        # A session's outputs are a directory, and a directory holds one file
        # per path. Enforced here rather than left to the code that captures
        # them: two captures of the same path racing each other would each
        # find no live row and each insert one, and the duplicate that
        # produces is invisible until someone looks at a file list and sees
        # the same name three times.
        #
        # Archived rows are excluded because they are the paths that no longer
        # exist. A file the agent deleted and later wrote again has to be able
        # to take its own name back.
        Index(
            "uq_files_live_scoped_path",
            "organization_id",
            "scope_id",
            "filename",
            unique=True,
            postgresql_where=text("archived_at IS NULL AND scope_id IS NOT NULL"),
            sqlite_where=text("archived_at IS NULL AND scope_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # The session that produced this file, or null if a user uploaded it.
    # Output files are not a separate kind of thing — they are ordinary files
    # wearing a label saying which run they came out of.
    scope_id: Mapped[str | None] = mapped_column(String(64))
    # For a captured output this is the path under the sandbox's `outputs/`,
    # so a file the agent left in a subdirectory keeps its place.
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # What the bytes hash to. For captured outputs this is also how a file that
    # has not changed since the last capture is recognised and left alone.
    sha256: Mapped[str | None] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
