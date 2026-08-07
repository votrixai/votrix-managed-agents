from datetime import datetime
from typing import Literal

from pydantic import Field

from app.models.common import ApiModel


# A skill is uploaded as multipart form data, not JSON, so there are no create
# or update request models — the zip and the fields arrive together in the same
# request. Going through the server rather than a presigned URL is deliberate:
# it is the only way anything can look inside the package before it is stored.


class SkillResponse(ApiModel):
    id: str
    type: Literal["skill"] = "skill"
    name: str
    description: str | None = None
    size_bytes: int
    sha256: str | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
