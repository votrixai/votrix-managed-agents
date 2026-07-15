from datetime import datetime
from typing import Literal

from pydantic import Field, SecretStr, field_validator

from app.models.common import ApiModel
from app.runtime.providers import RuntimeProviderCatalogEntry


class ModelProviderCapabilities(ApiModel):
    streaming: bool = Field(
        description="Whether this provider profile is declared to support streamed model output."
    )
    tool_calls: bool = Field(
        description="Whether this provider profile is declared to support model tool calls."
    )
    multimodal_input: bool = Field(
        description="Whether this provider profile accepts supported image or document model inputs."
    )
    reasoning: bool = Field(
        description="Whether this provider profile is declared to expose reasoning-capable models."
    )
    native_structured_output: bool = Field(
        description="Whether this provider profile is declared to support native structured output."
    )


class ModelProviderResponse(ApiModel):
    id: str
    type: Literal["model_provider"] = "model_provider"
    display_name: str = Field(
        description="Human-readable name of the server-approved model provider."
    )
    adapter: str = Field(
        description="Public adapter family used to construct this provider's chat model."
    )
    credential_type: Literal["api_key", "none"] = Field(
        description="Credential kind accepted by the provider through the native model-Credential API."
    )
    default_model: str | None = Field(
        default=None,
        description="Server-approved model used when an Agent does not select a model explicitly.",
    )
    capabilities: ModelProviderCapabilities = Field(
        description="Operator-declared runtime capabilities for this provider profile."
    )


class ModelCredentialCreateRequest(ApiModel):
    provider: str = Field(
        description="Stable provider ID returned by the model-provider catalog."
    )
    api_key: SecretStr = Field(
        description="Write-only provider API key; VMA never returns this value."
    )
    display_name: str | None = Field(
        default=None,
        description="Optional human-readable label for the model Credential.",
    )
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("api_key", mode="before")
    @classmethod
    def validate_api_key(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("api_key must be a non-empty string")
        return value


class ModelCredentialRotateRequest(ApiModel):
    api_key: SecretStr = Field(
        description="Replacement write-only provider API key; VMA never returns this value."
    )

    @field_validator("api_key", mode="before")
    @classmethod
    def validate_api_key(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("api_key must be a non-empty string")
        return value


class ModelCredentialResponse(ApiModel):
    id: str
    type: Literal["model_credential"] = "model_credential"
    vault_id: str = Field(
        description="Vault that owns this model Credential."
    )
    model_provider: str = Field(
        description="Canonical model-provider ID associated with this Credential."
    )
    display_name: str = Field(
        description="Human-readable label for this model Credential."
    )
    metadata: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class ModelCredentialDeletedResponse(ApiModel):
    id: str
    type: Literal["model_credential_deleted"] = "model_credential_deleted"
    deleted: Literal[True] = True


def model_provider_to_response(
    entry: RuntimeProviderCatalogEntry,
) -> ModelProviderResponse:
    return ModelProviderResponse(
        id=entry.id,
        display_name=entry.display_name,
        adapter=entry.adapter,
        credential_type=entry.credential_type,
        default_model=entry.default_model,
        capabilities=ModelProviderCapabilities(
            streaming=entry.capabilities.streaming,
            tool_calls=entry.capabilities.tool_calls,
            multimodal_input=entry.capabilities.multimodal_input,
            reasoning=entry.capabilities.reasoning,
            native_structured_output=entry.capabilities.native_structured_output,
        ),
    )
