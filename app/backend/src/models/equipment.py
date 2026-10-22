from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.backend.src.models.base import Base, BigIntPrimaryKey, TimestampMixin


class DeviceModel(TimestampMixin, Base):
    """An industrial device registered to a tenant."""

    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint("status IN ('online', 'offline', 'disabled')", name="status_allowed"),
        # 上报鉴权：用令牌哈希反查设备，必须唯一且带索引；
        # NULL 表示尚未签发令牌（PostgreSQL 的唯一索引允许多个 NULL，未签发设备不受影响）。
        Index("uq_devices_device_token_hash", "device_token_hash", unique=True),
    )

    id: Mapped[BigIntPrimaryKey]
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    device_code: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    device_name: Mapped[str | None] = mapped_column(String(255))
    device_model: Mapped[str | None] = mapped_column(String(128), index=True)
    serial_number: Mapped[str | None] = mapped_column(String(128))
    install_location: Mapped[str | None] = mapped_column(String(255))
    install_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="offline", server_default="offline"
    )
    last_online_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    device_token_hash: Mapped[str | None] = mapped_column(String(255))
