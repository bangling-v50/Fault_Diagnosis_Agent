from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class DiagnosisTaskModel(TimestampMixin, Base):
    """One fault-diagnosis attempt for a structured device alert."""

    __tablename__ = "diagnosis_tasks"
    __table_args__ = (
        CheckConstraint("trigger_type IN ('auto', 'manual', 'retry')", name="trigger_allowed"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="status_allowed",
        ),
        # 取值 = workflow 的真实节点名（见 agents/workflow.py）；改节点名要同步改这里
        CheckConstraint(
            "current_phase IS NULL OR current_phase IN "
            "('collect_knowledge', 'collect_alert_history', 'collect_work_orders',"
            " 'write_report', 'evaluate_report')",
            name="phase_allowed",
        ),
        Index("ix_diagnosis_tasks_tenant_status_created", "tenant_id", "status", "created_at"),
        Index("ix_diagnosis_tasks_alert_created", "alert_id", "created_at"),
    )

    id: Mapped[BigIntPrimaryKey]
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False
    )
    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id", ondelete="RESTRICT"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="QUEUED", server_default="QUEUED"
    )
    current_phase: Mapped[str | None] = mapped_column(String(32))
    input_payload: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    result_summary: Mapped[JsonObject | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DiagnosisStepModel(Base):
    """A high-level, explainable Plan-Execute-Replan step snapshot."""

    __tablename__ = "diagnosis_steps"
    __table_args__ = (
        UniqueConstraint("task_id", "sequence", name="uq_diagnosis_steps_task_sequence"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        # 同 tasks.current_phase：取值 = workflow 的真实节点名
        CheckConstraint(
            "phase IN ('collect_knowledge', 'collect_alert_history', 'collect_work_orders',"
            " 'write_report', 'evaluate_report')",
            name="phase_allowed",
        ),
        CheckConstraint("status IN ('RUNNING', 'SUCCEEDED', 'FAILED')", name="status_allowed"),
    )

    id: Mapped[BigIntPrimaryKey]
    task_id: Mapped[int] = mapped_column(
        ForeignKey("diagnosis_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[CreatedAt]


class DiagnosisEvidenceModel(Base):
    """An immutable evidence snapshot used by a diagnosis report."""

    __tablename__ = "diagnosis_evidence"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('alert', 'knowledge', 'alert_history', 'work_order')",
            name="kind_allowed",
        ),
        Index("ix_diagnosis_evidence_task_created", "task_id", "created_at"),
        Index("ix_diagnosis_evidence_source", "source_type", "source_id"),
    )

    id: Mapped[BigIntPrimaryKey]
    task_id: Mapped[int] = mapped_column(
        ForeignKey("diagnosis_tasks.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[int | None] = mapped_column(
        ForeignKey("diagnosis_steps.id", ondelete="SET NULL")
    )
    tool_call_id: Mapped[int | None] = mapped_column(
        ForeignKey("tool_call_audits.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_title: Mapped[str | None] = mapped_column(String(512))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[CreatedAt]


class DiagnosisReportModel(Base):
    """A versioned, structured fault-diagnosis report."""

    __tablename__ = "diagnosis_reports"
    __table_args__ = (
        UniqueConstraint("task_id", "version", name="uq_diagnosis_reports_task_version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "risk_level IS NULL OR risk_level IN ('low', 'medium', 'high')",
            name="risk_allowed",
        ),
        CheckConstraint(
            "need_shutdown IS NULL OR need_shutdown IN ('yes', 'no', 'conditional')",
            name="shutdown_allowed",
        ),
    )

    id: Mapped[BigIntPrimaryKey]
    task_id: Mapped[int] = mapped_column(
        ForeignKey("diagnosis_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_final: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    title: Mapped[str | None] = mapped_column(String(255))
    summary: Mapped[str | None] = mapped_column(Text)
    risk_level: Mapped[str | None] = mapped_column(String(32))
    possible_causes: Mapped[list[JsonObject]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    suggested_actions: Mapped[list[JsonObject]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    need_shutdown: Mapped[str | None] = mapped_column(String(32))
    need_field_service: Mapped[bool | None] = mapped_column(Boolean)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    llm_model: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[CreatedAt]


class ReportEvidenceLinkModel(Base):
    """A formal citation edge between a report version and an evidence snapshot."""

    __tablename__ = "report_evidence_links"
    __table_args__ = (
        UniqueConstraint(
            "report_id", "evidence_id", name="uq_report_evidence_links_report_evidence"
        ),
        CheckConstraint("citation_order >= 1", name="citation_order_positive"),
        Index("ix_report_evidence_links_report_order", "report_id", "citation_order"),
        Index("ix_report_evidence_links_evidence", "evidence_id"),
    )

    id: Mapped[BigIntPrimaryKey]
    report_id: Mapped[int] = mapped_column(
        ForeignKey("diagnosis_reports.id", ondelete="CASCADE"), nullable=False
    )
    evidence_id: Mapped[int] = mapped_column(
        ForeignKey("diagnosis_evidence.id", ondelete="RESTRICT"), nullable=False
    )
    citation_order: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[CreatedAt]


class DiagnosisCaseModel(Base):
    """An engineer-confirmed repair case eligible for knowledge indexing."""

    __tablename__ = "diagnosis_cases"
    __table_args__ = (
        CheckConstraint("scope IN ('global', 'tenant')", name="scope_allowed"),
        CheckConstraint(
            "(scope = 'global' AND tenant_id IS NULL) OR "
            "(scope = 'tenant' AND tenant_id IS NOT NULL)",
            name="scope_matches_tenant",
        ),
        Index("ix_diagnosis_cases_model_alert", "device_model", "alert_code"),
        Index("ix_diagnosis_cases_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[BigIntPrimaryKey]
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    device_model: Mapped[str] = mapped_column(String(128), nullable=False)
    alert_code: Mapped[str | None] = mapped_column(String(128))
    source_work_order_id: Mapped[int] = mapped_column(
        ForeignKey("work_orders.id", ondelete="RESTRICT"), nullable=False
    )
    source_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("diagnosis_tasks.id", ondelete="SET NULL")
    )
    source_report_id: Mapped[int | None] = mapped_column(
        ForeignKey("diagnosis_reports.id", ondelete="SET NULL")
    )
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    solution: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_result: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    case_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="SET NULL")
    )
    index_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_index_tasks.id", ondelete="SET NULL")
    )
    confirmed_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[CreatedAt]
