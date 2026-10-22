from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.backend.src.models.base import Base, BigIntPrimaryKey, TimestampMixin


class WorkOrderModel(TimestampMixin, Base):
    """A real maintenance work order used to close the diagnosis loop."""

    __tablename__ = "work_orders"
    __table_args__ = (
        CheckConstraint("priority IN ('low', 'medium', 'high', 'urgent')", name="priority_allowed"),
        CheckConstraint(
            "status IN ('OPEN', 'ASSIGNED', 'IN_PROGRESS', 'RESOLVED', 'CLOSED')",
            name="status_allowed",
        ),
        Index("ix_work_orders_tenant_device_status", "tenant_id", "device_id", "status"),
        Index("ix_work_orders_device_alert", "device_id", "alert_id"),
        Index("ix_work_orders_closed_at", "closed_at"),
    )

    id: Mapped[BigIntPrimaryKey]
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False
    )
    alert_id: Mapped[int | None] = mapped_column(ForeignKey("alerts.id", ondelete="SET NULL"))
    diagnosis_report_id: Mapped[int | None] = mapped_column(
        ForeignKey("diagnosis_reports.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(
        String(32), nullable=False, default="medium", server_default="medium"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="OPEN", server_default="OPEN"
    )
    assigned_to: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    confirmed_root_cause: Mapped[str | None] = mapped_column(Text)
    solution: Mapped[str | None] = mapped_column(Text)
    resolution_result: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
