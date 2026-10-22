from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.backend.src.models.base import Base, BigIntPrimaryKey, CreatedAt, TimestampMixin


class TenantModel(TimestampMixin, Base):
    """An enterprise or factory tenant."""

    __tablename__ = "tenants"
    __table_args__ = (CheckConstraint("status IN ('active', 'inactive')", name="status_allowed"),)

    id: Mapped[BigIntPrimaryKey]
    tenant_code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    tenant_name: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str | None] = mapped_column(String(128))
    address: Mapped[str | None] = mapped_column(Text)
    contact_person: Mapped[str | None] = mapped_column(String(128))
    contact_phone: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )


class UserModel(TimestampMixin, Base):
    """An authenticated FaDia user."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('operator', 'engineer', 'admin')",
            name="role_allowed",
        ),
        CheckConstraint("status IN ('active', 'disabled')", name="status_allowed"),
    )

    id: Mapped[BigIntPrimaryKey]
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    real_name: Mapped[str | None] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(
        String(64), nullable=False, default="operator", server_default="operator"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )


class AuthSessionModel(Base):
    """A revocable login session; only the token hash is persisted."""

    __tablename__ = "auth_sessions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    created_at: Mapped[CreatedAt]
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
