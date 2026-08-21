"""Billing Accounts and the model credentials they spend through.

An Account is the boundary an Organization's usage is attributed to. Platform
Accounts own one managed OpenRouter credential; BYOK Accounts may own one
direct credential per supported model backend. A request either resolves a
credential inside its pinned Account or fails, so usage cannot silently move
to a different billing boundary.

Secret material lives only on ``AccountModelCredential`` and only encrypted.
Nothing here ever holds a plaintext key.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base, TimestampMixin

ACCOUNT_PROVISIONING = "provisioning"
ACCOUNT_ACTIVE = "active"
ACCOUNT_SUSPENDED = "suspended"
ACCOUNT_STATUSES = (ACCOUNT_PROVISIONING, ACCOUNT_ACTIVE, ACCOUNT_SUSPENDED)

FUNDING_PLATFORM = "platform"
FUNDING_BYOK = "byok"
FUNDING_MODES = (FUNDING_PLATFORM, FUNDING_BYOK)

CREDENTIAL_OPENROUTER = "openrouter"
CREDENTIAL_ANTHROPIC = "anthropic"
CREDENTIAL_OPENAI = "openai"
CREDENTIAL_GOOGLE = "google"
CREDENTIAL_DEEPSEEK = "deepseek"
CREDENTIAL_PROVIDERS = (
    CREDENTIAL_OPENROUTER,
    CREDENTIAL_ANTHROPIC,
    CREDENTIAL_OPENAI,
    CREDENTIAL_GOOGLE,
    CREDENTIAL_DEEPSEEK,
)
DIRECT_CREDENTIAL_PROVIDERS = (
    CREDENTIAL_ANTHROPIC,
    CREDENTIAL_OPENAI,
    CREDENTIAL_GOOGLE,
    CREDENTIAL_DEEPSEEK,
)

CREDENTIAL_ACTIVE = "active"
CREDENTIAL_SUSPENDED = "suspended"
CREDENTIAL_STATUSES = (CREDENTIAL_ACTIVE, CREDENTIAL_SUSPENDED)


class OrganizationAccount(TimestampMixin, Base):
    __tablename__ = "organization_accounts"
    __table_args__ = (
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
        # Referenced by the credential's composite FK. It makes funding mode an
        # immutable part of the Account/credential relationship in the DB, not
        # merely a convention shared by two rows.
        UniqueConstraint(
            "id",
            "funding_mode",
            name="uq_organization_accounts_id_funding_mode",
        ),
        CheckConstraint(
            "status IN ('provisioning', 'active', 'suspended')",
            name="ck_organization_accounts_status",
        ),
        CheckConstraint(
            "limit_usd IS NULL OR limit_usd > 0",
            name="ck_organization_accounts_limit_positive",
        ),
        CheckConstraint(
            "funding_mode IN ('platform', 'byok')",
            name="ck_organization_accounts_funding_mode",
        ),
        CheckConstraint(
            "funding_mode != 'byok' OR limit_usd IS NULL",
            name="ck_organization_accounts_byok_no_limit",
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
    funding_mode: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=FUNDING_PLATFORM,
        server_default=FUNDING_PLATFORM,
    )
    # NULL rather than False for a non-default, so the unique constraint above
    # permits many non-defaults and exactly one default.
    is_default: Mapped[bool | None] = mapped_column(Boolean)
    limit_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))

    model_credentials: Mapped[list["AccountModelCredential"]] = relationship(
        back_populates="account",
        foreign_keys="AccountModelCredential.account_id",
        lazy="selectin",
        order_by="AccountModelCredential.backend",
    )


class AccountModelCredential(TimestampMixin, Base):
    """One model backend credential belonging to an Account.

    Platform rows are managed OpenRouter keys and carry a provider-side name.
    BYOK rows are user-owned direct-provider keys and deliberately carry no
    managed name. ``funding_mode`` is repeated solely so a composite foreign
    key can make that distinction agree with the parent Account in the DB.
    """

    __tablename__ = "account_model_credentials"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "funding_mode"],
            ["organization_accounts.id", "organization_accounts.funding_mode"],
            name="fk_account_model_credentials_account_funding",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "account_id",
            "backend",
            name="uq_account_model_credentials_account_backend",
        ),
        UniqueConstraint(
            "key_hash",
            name="uq_account_model_credentials_key_hash",
        ),
        UniqueConstraint(
            "provider_key_name",
            name="uq_account_model_credentials_key_name",
        ),
        CheckConstraint(
            "status IN ('active', 'suspended')",
            name="ck_account_model_credentials_status",
        ),
        CheckConstraint(
            "funding_mode IN ('platform', 'byok')",
            name="ck_account_model_credentials_funding_mode",
        ),
        CheckConstraint(
            "backend IN "
            "('openrouter', 'anthropic', 'openai', 'google', 'deepseek')",
            name="ck_account_model_credentials_backend",
        ),
        CheckConstraint(
            "(funding_mode = 'platform' AND backend = 'openrouter' "
            "AND provider_key_name IS NOT NULL) OR "
            "(funding_mode = 'byok' AND backend IN "
            "('anthropic', 'openai', 'google', 'deepseek') "
            "AND provider_key_name IS NULL)",
            name="ck_account_model_credentials_funding_backend",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("organization_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    funding_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=FUNDING_PLATFORM
    )
    backend: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=CREDENTIAL_OPENROUTER
    )
    organization_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # OpenRouter's key hash for a managed credential; a provider-scoped
    # one-way fingerprint for BYOK. Both are stable non-secret identities.
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_key_name: Mapped[str | None] = mapped_column(String(255))
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CREDENTIAL_ACTIVE
    )
    generation: Mapped[int] = mapped_column(nullable=False, default=1)

    account: Mapped[OrganizationAccount] = relationship(
        back_populates="model_credentials",
        foreign_keys=[account_id],
    )


__all__ = [
    "ACCOUNT_ACTIVE",
    "ACCOUNT_PROVISIONING",
    "ACCOUNT_STATUSES",
    "ACCOUNT_SUSPENDED",
    "CREDENTIAL_ACTIVE",
    "CREDENTIAL_ANTHROPIC",
    "CREDENTIAL_DEEPSEEK",
    "CREDENTIAL_GOOGLE",
    "CREDENTIAL_OPENAI",
    "CREDENTIAL_OPENROUTER",
    "CREDENTIAL_PROVIDERS",
    "CREDENTIAL_STATUSES",
    "CREDENTIAL_SUSPENDED",
    "DIRECT_CREDENTIAL_PROVIDERS",
    "FUNDING_BYOK",
    "FUNDING_MODES",
    "FUNDING_PLATFORM",
    "AccountModelCredential",
    "OrganizationAccount",
]
