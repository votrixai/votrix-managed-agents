"""Request and response bodies for billing Accounts."""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime
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


class AccountUsageResponse(ApiModel):
    """What an Account has spent, in USD, as the provider counts it.

    Read from the provider rather than accumulated here, so it includes
    everything the Account's credential was charged for — not only the calls
    this platform observed.

    Every figure is cumulative within its window and updates as calls complete.
    The period figures reset on the provider's UTC boundaries; `usage_usd` never
    resets, which is what makes a difference between two readings meaningful.
    """

    account_id: str
    type: Literal["account_usage"] = "account_usage"
    # The Account's whole life. Bill from differences between readings of this
    # rather than from the period figures, whose resets fall on UTC boundaries
    # that are unlikely to be anyone's billing period.
    usage_usd: Decimal
    usage_daily_usd: Decimal
    usage_weekly_usd: Decimal
    usage_monthly_usd: Decimal
    limit_usd: Decimal | None = None
    limit_remaining_usd: Decimal | None = None


class AccountUsageSummary(ApiModel):
    account_id: str
    name: str
    status: Literal["provisioning", "active", "suspended"]
    is_default: bool
    usage_usd: Decimal
    usage_daily_usd: Decimal
    usage_weekly_usd: Decimal
    usage_monthly_usd: Decimal


class OrganizationUsageResponse(ApiModel):
    organization_id: str
    type: Literal["organization_usage"] = "organization_usage"
    usage_usd: Decimal
    usage_daily_usd: Decimal
    usage_weekly_usd: Decimal
    usage_monthly_usd: Decimal
    as_of: datetime
    accounts: list[AccountUsageSummary]


__all__ = [
    "AccountCreateRequest",
    "AccountResponse",
    "AccountUsageResponse",
    "AccountUsageSummary",
    "OrganizationUsageResponse",
]
