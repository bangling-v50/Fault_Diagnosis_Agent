from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.backend.src.models.base import (
    Base,
    BigIntPrimaryKey,
    CreatedAt,
    JsonObject,
    TimestampMixin,
)


class BackgroundJobModel(TimestampMixin, Base):
    """A durable background job with a renewable lease.

    Kind 的三条线：诊断 / 文档索引 / 聊天记忆提炼。
    """

    __tablename__ = "background_jobs"
    __table_args__ = (
        # 加新 kind 要同步改这里 + services/job_service.py:VALID_KINDS + 一次 ALTER
        CheckConstraint(
            "kind IN ('diagnosis', 'document_index', 'chat_memory_extract', 'multimodal_index')",
            name="kind_allowed",
        ),
        CheckConstraint(
            "resource_type IN ('diagnosis_task', 'document', 'chat_session')",
            name="resource_type_allowed",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="status_allowed",
        ),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("timeout_seconds > 0", name="timeout_positive"),
        Index("ix_background_jobs_status_available", "status", "available_at"),
        Index("ix_background_jobs_tenant_created", "tenant_id", "created_at"),
        Index("ix_background_jobs_resource", "resource_type", "resource_id"),
    )

    id: Mapped[BigIntPrimaryKey]
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="QUEUED", server_default="QUEUED"
    )
    payload: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=900, server_default="900"
    )
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_of_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("background_jobs.id", ondelete="SET NULL")
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BackgroundJobEventModel(Base):
    """An immutable, ordered progress event for a background job."""

    __tablename__ = "background_job_events"
    __table_args__ = (
        UniqueConstraint("job_id", "sequence", name="uq_background_job_events_sequence"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint(
            "event_type IN ('status', 'progress', 'error', 'result')",
            name="event_type_allowed",
        ),
    )

    id: Mapped[BigIntPrimaryKey]
    job_id: Mapped[int] = mapped_column(
        ForeignKey("background_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[CreatedAt]
