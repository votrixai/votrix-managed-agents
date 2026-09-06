from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_serializer, model_validator

from app.models.common import ApiModel

# The managers that get a first-class shorthand on a build step. apt is listed
# first only so that a legacy `packages` block translates into steps in the
# order it used to build in, where apt ran before the managers relying on it.
PACKAGE_MANAGERS = ("apt", "cargo", "gem", "go", "npm", "pip")


class BuildStep(ApiModel):
    """One step of an image build, run in the order the steps are listed.

    Exactly one field is set. `run` is the general form — a command run as root,
    which covers any manager not listed below (brew, apk, uv, …), adding a
    package source before installing from it, or a one-time initialization a
    just-installed tool needs before first use. The rest are shorthands for the
    common managers, kept only so the frequent case stays readable; each takes
    the manager's own version syntax — `pandas==2.2.0`, `express@4.18.0`.
    """

    run: str | None = None
    apt: list[str] | None = None
    cargo: list[str] | None = None
    gem: list[str] | None = None
    go: list[str] | None = None
    npm: list[str] | None = None
    pip: list[str] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "BuildStep":
        chosen = [f for f in ("run", *PACKAGE_MANAGERS) if getattr(self, f) is not None]
        if len(chosen) != 1:
            raise ValueError(
                "a build step sets exactly one of: run, " + ", ".join(PACKAGE_MANAGERS)
            )
        return self

    @model_serializer
    def _only_the_field_set(self) -> dict[str, Any]:
        """Serialize just the one field in use, not the six unset ones — a step
        reads back as `{"apt": [...]}`, not padded out with five nulls."""
        return {f: v for f in ("run", *PACKAGE_MANAGERS) if (v := getattr(self, f)) is not None}


class EnvironmentConfig(ApiModel):
    """The machine, and what is built onto it.

    Both are baked into the image, so both are decided here rather than per
    session — a headless browser and a spreadsheet script want very different
    machines, and every session on this environment gets what it asks for.

    `steps` run in order, so a later one can rely on what an earlier one
    installed — which is what lets a step add a package source, the next install
    from it, and a last one initialize the tool it just brought in.
    """

    steps: list[BuildStep] = Field(default_factory=list)
    cpu: int = Field(default=2, ge=1, le=8)
    memory_mb: int = Field(default=1024, ge=512, le=8192)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_packages(cls, data: Any) -> Any:
        """Read the retired `packages: {manager: [...]}` shape as `steps`.

        Environments created before build steps existed stored a `packages`
        block, and older clients still send one. Translating it here — in the
        fixed manager order it used to build in — lets those rows and those
        callers keep working with no data migration, while everything
        downstream only ever sees `steps`. An explicit `steps` wins outright.
        """
        if not isinstance(data, dict) or "packages" not in data:
            return data
        rest = {k: v for k, v in data.items() if k != "packages"}
        if "steps" in rest:
            return rest
        packages = data.get("packages") or {}
        rest["steps"] = [
            {manager: entries}
            for manager in PACKAGE_MANAGERS
            if (entries := packages.get(manager))
        ]
        return rest


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
