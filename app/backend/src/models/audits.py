from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.backend.src.models.base import Base, BigIntPrimaryKey, CreatedAt, JsonObject


class ToolCallAuditModel(Base):
    """An immutable tool call owned by exactly one chat or diagnosis task."""

    __tablename__ = "tool_call_audits"
    __table_args__ = (
        CheckConstraint(
            "(chat_session_id IS NOT NULL AND diagnosis_task_id IS NULL) OR "
            "(chat_session_id IS NULL AND diagnosis_task_id IS NOT NULL)",
            name="exactly_one_parent",
        ),
        CheckConstraint("status IN ('RUNNING', 'SUCCEEDED', 'FAILED')", name="status_allowed"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration_non_negative"),
        Index("ix_tool_call_audits_chat_created", "chat_session_id", "created_at"),
        Index("ix_tool_call_audits_diagnosis_created", "diagnosis_task_id", "created_at"),
    )

    id: Mapped[BigIntPrimaryKey]
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    chat_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE")
    )
    chat_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL")
    )
    diagnosis_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("diagnosis_tasks.id", ondelete="CASCADE")
    )
    diagnosis_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("diagnosis_steps.id", ondelete="SET NULL")
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text)
    result_payload: Mapped[JsonObject | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[CreatedAt]
