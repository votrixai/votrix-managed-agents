"""Request and response bodies for billing Accounts."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, model_validator

from app.models.common import ApiModel

DirectAccountBackend = Literal["anthropic", "openai", "google", "deepseek"]


class PlatformFundingRequest(ApiModel):
    """A credential VMA mints and administers for this Account."""

    type: Literal["platform"] = "platform"
    backend: Literal["openrouter"] = "openrouter"


class ByokModelCredentialRequest(ApiModel):
    """One direct-provider API key supplied for a BYOK Account."""

    backend: DirectAccountBackend
    api_key: SecretStr = Field(
        min_length=1,
        description=(
            "The API key for this direct model backend. It is validated, "
            "encrypted, and never returned by the API."
        ),
    )


class ByokModelCredentialSetRequest(ApiModel):
    """A write-only direct-provider key to add or replace."""

    api_key: SecretStr = Field(
        min_length=1,
        description=(
            "The replacement API key for the backend named in the URL. It is "
            "validated, encrypted, and never returned by the API."
        ),
    )


class ByokFundingRequest(ApiModel):
    """User-owned direct model credentials grouped under one Account."""

    type: Literal["byok"] = "byok"
    credentials: list[ByokModelCredentialRequest] = Field(
        min_length=1,
        max_length=4,
        description=(
            "One API key per direct model backend. OpenRouter is reserved for "
            "Platform funding."
        ),
    )

    @model_validator(mode="after")
    def credentials_have_unique_backends(self) -> Self:
        backends = [credential.backend for credential in self.credentials]
        if len(backends) != len(set(backends)):
            raise ValueError("A BYOK Account accepts only one key per backend")
        return self


AccountFundingRequest = Annotated[
    PlatformFundingRequest | ByokFundingRequest,
    Field(discriminator="type"),
]


class PlatformFundingResponse(ApiModel):
    type: Literal["platform"] = Field(
        default="platform", description="VMA supplies and administers the credential."
    )
    backend: Literal["openrouter"] = Field(
        default="openrouter", description="The inference API used by this Account."
    )


class ByokModelCredentialResponse(ApiModel):
    backend: DirectAccountBackend = Field(
        description="A direct model backend configured on this Account."
    )


class ByokFundingResponse(ApiModel):
    type: Literal["byok"] = Field(
        default="byok", description="The Account uses user-owned credentials."
    )
    credentials: list[ByokModelCredentialResponse] = Field(
        description="Configured direct backends. Secret values are never returned."
    )


AccountFundingResponse = Annotated[
    PlatformFundingResponse | ByokFundingResponse,
    Field(discriminator="type"),
]


class AccountCreateRequest(ApiModel):
    name: str = Field(
        max_length=255,
        description="What this Account is called in listings and usage views.",
    )
    limit_usd: Decimal | None = Field(
        default=None,
        gt=0,
        description=(
            "Hard spending cap in USD for Platform funding. Omit to leave the "
            "Platform Account uncapped."
        ),
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=255,
        description=(
            "Repeat this on a retry to get back the Account the first attempt "
            "made, instead of a second one billing the same thing."
        ),
    )
    funding: AccountFundingRequest = Field(
        default_factory=PlatformFundingRequest,
        description=(
            "Who supplies this Account's model credentials. Omit for a "
            "VMA-managed OpenRouter key; use `byok` for direct-provider keys."
        ),
    )

    @model_validator(mode="after")
    def validate_funding_limit(self) -> Self:
        if self.funding.type == "byok" and self.limit_usd is not None:
            raise ValueError(
                "limit_usd is only available for Platform-funded Accounts"
            )
        return self


class AccountResponse(ApiModel):
    """One isolated boundary for credential routing and usage attribution."""

    id: str = Field(description="Stable Account identifier.")
    type: Literal["account"] = Field(
        default="account", description="The resource type."
    )
    organization_id: str = Field(description="Organization that owns the Account.")
    name: str = Field(description="Human-readable Account name.")
    status: Literal["provisioning", "active", "suspended"] = Field(
        description="Whether this Account can currently fund model calls."
    )
    is_default: bool = Field(
        description="Whether requests without an account_id resolve here."
    )
    limit_usd: Decimal | None = Field(
        default=None,
        description="Provider-enforced USD cap for Platform Accounts, if configured.",
    )
    funding: AccountFundingResponse = Field(
        description="Who supplies the model credentials and their backends."
    )


class ObservedTokenUsage(ApiModel):
    """Normalized tokens from model calls VMA recorded for this Account."""

    input_tokens: int = Field(
        ge=0, description="Input tokens observed across completed model calls."
    )
    output_tokens: int = Field(
        ge=0, description="Output tokens observed across completed model calls."
    )
    total_tokens: int = Field(
        ge=0, description="Total tokens reported across completed model calls."
    )


class AccountUsageResponse(ApiModel):
    """External USD billing where available, plus usage observed by VMA.

    Platform USD figures come from the isolated managed OpenRouter key. Direct
    BYOK backends do not expose one common Account-scoped billing contract, so
    their USD figures are null. ``observed_usage`` remains available for both.
    """

    account_id: str = Field(description="Account these figures belong to.")
    type: Literal["account_usage"] = Field(
        default="account_usage", description="The resource type."
    )
    funding: AccountFundingResponse = Field(
        description="Funding and backend context for interpreting these figures."
    )
    usage_usd: Decimal | None = Field(
        default=None,
        description="OpenRouter lifetime USD usage; unavailable for BYOK.",
    )
    usage_daily_usd: Decimal | None = Field(
        default=None,
        description="OpenRouter daily USD usage; unavailable for BYOK.",
    )
    usage_weekly_usd: Decimal | None = Field(
        default=None,
        description="OpenRouter weekly USD usage; unavailable for BYOK.",
    )
    usage_monthly_usd: Decimal | None = Field(
        default=None,
        description="OpenRouter monthly USD usage; unavailable for BYOK.",
    )
    limit_usd: Decimal | None = Field(
        default=None, description="Provider-enforced Platform Account limit."
    )
    limit_remaining_usd: Decimal | None = Field(
        default=None,
        description="Provider-reported remaining Platform Account limit.",
    )
    observed_usage: ObservedTokenUsage = Field(
        description="Provider-neutral tokens from calls VMA observed for this Account."
    )


__all__ = [
    "AccountCreateRequest",
    "AccountFundingRequest",
    "AccountFundingResponse",
    "AccountResponse",
    "AccountUsageResponse",
    "ByokFundingRequest",
    "ByokFundingResponse",
    "ByokModelCredentialRequest",
    "ByokModelCredentialResponse",
    "ByokModelCredentialSetRequest",
    "DirectAccountBackend",
    "ObservedTokenUsage",
    "PlatformFundingRequest",
    "PlatformFundingResponse",
]
