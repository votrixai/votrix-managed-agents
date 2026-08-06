"""Request and response bodies for billing Accounts."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field

from app.models.common import ApiModel


class AccountCreateRequest(ApiModel):
    name: str = Field(
        max_length=255,
        description="What this Account is called in listings and provider exports.",
    )
    limit_usd: Decimal | None = Field(
        default=None,
        gt=0,
        description=(
            "Hard spending cap in USD, enforced by the provider rather than by "
            "us — it holds even when our own metering does not. Omit to leave "
            "the Account uncapped."
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


class AccountResponse(ApiModel):
    id: str
    type: Literal["account"] = "account"
    organization_id: str
    name: str
    # `provisioning` means the credential is not in place yet, so the Account
    # cannot be spent through. `suspended` means it no longer can.
    status: Literal["provisioning", "active", "suspended"]
    is_default: bool
    limit_usd: Decimal | None = None


__all__ = ["AccountCreateRequest", "AccountResponse"]
