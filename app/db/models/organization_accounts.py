"""Billing Accounts and the provider credential each one spends through.

An Account is the boundary an Organization's spend is measured and capped at.
One Account holds one provider credential, and that credential is the whole
reason the boundary is enforceable: a request either carries it or fails, so
spend cannot land on an Account by mistake the way a mislabelled request can
land under the wrong tag.

Secret material lives only on ``AccountProviderCredential`` and only encrypted.
Nothing here ever holds a plaintext key.

Accounts are never removed. Suspending one stops it spending while leaving
every figure recorded against it readable, which is what a billing record has
to do — a deleted Account takes the history of what it was charged with it.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base, TimestampMixin

# An Account exists before its credential does: minting one is a call to
# another service, and that call can fail after the row is written. The state
# says which of those happened, so a retry knows whether to mint or to adopt.
ACCOUNT_PROVISIONING = "provisioning"
ACCOUNT_ACTIVE = "active"
ACCOUNT_SUSPENDED = "suspended"
ACCOUNT_STATUSES = (ACCOUNT_PROVISIONING, ACCOUNT_ACTIVE, ACCOUNT_SUSPENDED)

CREDENTIAL_ACTIVE = "active"
CREDENTIAL_SUSPENDED = "suspended"
CREDENTIAL_STATUSES = (CREDENTIAL_ACTIVE, CREDENTIAL_SUSPENDED)


class OrganizationAccount(TimestampMixin, Base):
    __tablename__ = "organization_accounts"
    __table_args__ = (
        # One default per Organization, enforced here rather than in code: the
        # default is what a request without an Account resolves to, and two of
        # them is a coin toss over which one gets billed.
        UniqueConstraint(
            "organization_id",
            "is_default",
            name="uq_organization_accounts_single_default",
            postgresql_nulls_not_distinct=False,
        ),
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_organization_accounts_idempotency_key",
        ),
        CheckConstraint(
            "status IN ('provisioning', 'active', 'suspended')",
            name="ck_organization_accounts_status",
        ),
        CheckConstraint(
            "limit_usd IS NULL OR limit_usd > 0",
            name="ck_organization_accounts_limit_positive",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ACCOUNT_PROVISIONING
    )
    # NULL rather than False for a non-default, so the unique constraint above
    # permits many non-defaults and exactly one default.
    is_default: Mapped[bool | None] = mapped_column(Boolean)
    # Uncapped unless a limit is asked for. A cap is the one control the
    # provider enforces on our behalf, and imposing an unrequested one would
    # fail requests nobody asked to have failed.
    limit_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    # Scoped per Organization by the constraint above: one tenant's key must
    # not collide with another's.
    idempotency_key: Mapped[str | None] = mapped_column(String(255))

    credential: Mapped["AccountProviderCredential | None"] = relationship(
        back_populates="account",
        uselist=False,
        lazy="selectin",
    )


class AccountProviderCredential(TimestampMixin, Base):
    """What one Account spends through.

    ``encrypted_key`` is decrypted at the outbound inference boundary and
    nowhere else. ``provider_key_name`` is the only attribution readable from
    the provider's side: a console or billing export shows the name, and
    without the Account id in it a key there cannot be traced back to what it
    bills.
    """

    __tablename__ = "account_provider_credentials"
    __table_args__ = (
        UniqueConstraint("account_id", name="uq_account_provider_credentials_account"),
        UniqueConstraint("key_hash", name="uq_account_provider_credentials_key_hash"),
        UniqueConstraint(
            "provider_key_name",
            name="uq_account_provider_credentials_key_name",
        ),
        CheckConstraint(
            "status IN ('active', 'suspended')",
            name="ck_account_provider_credentials_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("organization_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_key_name: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CREDENTIAL_ACTIVE
    )
    # Rotation is not implemented here, but the name carries the number so a
    # rotated key is distinguishable from the one it replaced. A provider-side
    # name is forever: it is what a historical billing export says.
    generation: Mapped[int] = mapped_column(nullable=False, default=1)

    account: Mapped[OrganizationAccount] = relationship(back_populates="credential")


__all__ = [
    "ACCOUNT_ACTIVE",
    "ACCOUNT_PROVISIONING",
    "ACCOUNT_STATUSES",
    "ACCOUNT_SUSPENDED",
    "CREDENTIAL_ACTIVE",
    "CREDENTIAL_STATUSES",
    "CREDENTIAL_SUSPENDED",
    "AccountProviderCredential",
    "OrganizationAccount",
]
