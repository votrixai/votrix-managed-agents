from datetime import datetime
from typing import Literal

from pydantic import Field

from app.models.common import ApiModel

# Run in this order, which is alphabetical — system packages happen to come
# first, so a later manager can rely on the toolchain they bring in.
PACKAGE_MANAGERS = ("apt", "cargo", "gem", "go", "npm", "pip")


class PackagesConfig(ApiModel):
    """What to install into the image.

    Versions use each manager's own syntax — `pandas==2.2.0`, `express@4.18.0`,
    `rails:7.1.0`. Unpinned entries take the latest.
    """

    apt: list[str] = Field(default_factory=list)
    cargo: list[str] = Field(default_factory=list)
    gem: list[str] = Field(default_factory=list)
    go: list[str] = Field(default_factory=list)
    npm: list[str] = Field(default_factory=list)
    pip: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(getattr(self, manager) for manager in PACKAGE_MANAGERS)


class EnvironmentConfig(ApiModel):
    """The machine, and what is installed on it.

    Both are baked into the image, so both are decided here rather than per
    session — a headless browser and a spreadsheet script want very different
    machines, and every session on this environment gets what it asks for.
    """

    packages: PackagesConfig = Field(default_factory=PackagesConfig)
    cpu: int = Field(default=2, ge=1, le=8)
    memory_mb: int = Field(default=1024, ge=512, le=8192)


class EnvironmentCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    config: EnvironmentConfig = Field(default_factory=EnvironmentConfig)


class EnvironmentUpdateRequest(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    config: EnvironmentConfig | None = None


class EnvironmentResponse(ApiModel):
    id: str
    type: Literal["environment"] = "environment"
    name: str
    description: str | None = None
    config: EnvironmentConfig
    # `building` until the image finishes; `failed` puts the reason in
    # `build_error`. A session cannot start until this reads `ready`.
    build_state: str
    build_error: str | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
