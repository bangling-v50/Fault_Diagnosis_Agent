from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.backend.src.models.base import Base, BigIntPrimaryKey, JsonObject, TimestampMixin


class AlertModel(TimestampMixin, Base):
    """A structured device alert consumed by the diagnosis agent."""

    __tablename__ = "alerts"
    __table_args__ = (
        CheckConstraint("alert_level IN ('info', 'warning', 'critical')", name="level_allowed"),
        CheckConstraint("occurrence_count >= 1", name="occurrence_count_positive"),
        Index(
            "ix_alerts_tenant_device_code_last_seen",
            "tenant_id",
            "device_id",
            "alert_code",
            "last_seen_at",
        ),
        Index("ix_alerts_tenant_status_created", "tenant_id", "status", "created_at"),
    )

    id: Mapped[BigIntPrimaryKey]
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False
    )
    alert_code: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    alert_name: Mapped[str | None] = mapped_column(String(255))
    alert_level: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="NEW", server_default="NEW"
    )
    source: Mapped[str | None] = mapped_column(String(128))
    raw_message: Mapped[str | None] = mapped_column(Text)
    diagnosis_type: Mapped[str | None] = mapped_column(String(128))
    diagnosis_score: Mapped[float | None] = mapped_column(Float)
    sensor_summary: Mapped[JsonObject | None] = mapped_column(JSONB)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurrence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
