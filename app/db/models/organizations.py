from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, TimestampMixin


MEMBER_ROLE_OWNER = "owner"
MEMBER_ROLE_ADMIN = "admin"
MEMBER_ROLE_MEMBER = "member"
MEMBER_ROLES = frozenset(
    {
        MEMBER_ROLE_OWNER,
        MEMBER_ROLE_ADMIN,
        MEMBER_ROLE_MEMBER,
    }
)


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrganizationMember(TimestampMixin, Base):
    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_id",
            name="uq_organization_members_organization_user",
        ),
        CheckConstraint(
            "role IN ('owner', 'admin', 'member')",
            name="ck_organization_members_role",
        ),
        Index("ix_organization_members_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "organizations.id",
            name="fk_organization_members_organization",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=MEMBER_ROLE_MEMBER,
    )


class OrganizationOnboardingRequest(TimestampMixin, Base):
    """One resumable self-service Organization creation per Supabase user.

    The Organization and its provider credential cannot be written atomically:
    OpenRouter is a second system. Keeping the request in VMA lets a browser or
    function retry an ambiguous response without minting another Organization
    or another provider key. Membership is granted only after the default
    Account is active, so a half-provisioned Organization never appears in the
    Developer Console.
    """

    __tablename__ = "organization_onboarding_requests"
    __table_args__ = (
        UniqueConstraint(
            "requester_user_id",
            name="uq_organization_onboarding_requests_user",
        ),
        UniqueConstraint(
            "organization_id",
            name="uq_organization_onboarding_requests_organization",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    requester_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requester_email: Mapped[str | None] = mapped_column(String(255))
    requested_name: Mapped[str] = mapped_column(String(255), nullable=False)
    organization_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "organizations.id",
            name="fk_organization_onboarding_requests_organization",
            ondelete="RESTRICT",
        ),
    )
    provisioning_lease_token: Mapped[str | None] = mapped_column(String(64))
    provisioning_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
