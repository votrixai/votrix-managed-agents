from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, Field

from app.models.common import ApiModel


class VaultCreateRequest(ApiModel):
    display_name: str = Field(
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("display_name", "name"),
        description="Human-readable Vault name. The legacy `name` input alias is also accepted.",
    )
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Caller-defined string metadata stored with the Vault.",
    )


class VaultUpdateRequest(ApiModel):
    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("display_name", "name"),
        description="Replacement human-readable Vault name. The legacy `name` input alias is also accepted.",
    )
    metadata: dict[str, str | None] | None = Field(
        default=None,
        description="Metadata patch; null or empty-string values remove existing keys.",
    )


class VaultResponse(ApiModel):
    id: str
    type: Literal["vault"] = "vault"
    name: str = Field(description="Canonical stored name for the Vault.")
    display_name: str = Field(description="Human-readable Vault name.")
    metadata: dict[str, str] = Field(default_factory=dict)
    status: Literal["active", "archived"] = Field(description="Current Vault lifecycle status.")
    archived_at: datetime | None = None
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class VaultDeletedResponse(ApiModel):
    id: str
    type: Literal["vault_deleted"] = "vault_deleted"
    deleted: Literal[True] = True
