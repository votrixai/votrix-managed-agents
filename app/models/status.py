from typing import Literal

from pydantic import Field

from app.models.common import ApiModel


class HealthResponse(ApiModel):
    status: Literal["ok"] = Field(description="Application health state.")


class DatabaseHealthResponse(HealthResponse):
    db: Literal["ok"] = Field(description="Database connectivity state.")


class CapabilityManifestResponse(ApiModel):
    type: Literal["capability_manifest"] = "capability_manifest"
    release_channel: Literal["public_beta"] = Field(description="Release channel represented by this manifest.")
    resources: dict[str, Literal["ga"]] = Field(
        description="Public resources supported by this release channel.",
    )
    platform_guarantees: dict[str, Literal["ga"]] = Field(
        description="Cross-cutting public-beta guarantees enforced by the platform.",
    )
    deferred: dict[str, Literal["unsupported"]] = Field(
        description="Capabilities intentionally unavailable in this release channel.",
    )
