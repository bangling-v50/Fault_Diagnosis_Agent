from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DiagnosisTaskPublic(BaseModel):
    """诊断任务（列表项；不含触发快照，那个只在详情里给）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int
    device_id: int
    alert_id: int
    trigger_type: str
    status: str
    current_phase: str | None
    # workflow 收尾时写下的结论摘要：报告 id/版本、评审结论（verdict/score/issues）、
    # 证据条数、采集过程中的问题（collectionNotes）
    result_summary: dict[str, Any] | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    # 由列表查询 join 出来、方便列表页直接显示（不是 diagnosis_tasks 的列）
    device_code: str | None = None
    alert_code: str | None = None


class DiagnosisTaskDetail(DiagnosisTaskPublic):
    """任务详情：比列表项多一份**触发时的快照**。

    快照取自 ``diagnosis_tasks.input_payload``（入队时就存好的告警与设备信息）——它是
    "当时看到的是什么"，即使事后告警被合并计数、设备被改名，回放出来的仍是那一刻的事实。
    """

    input_snapshot: dict[str, Any] | None = None


class DiagnosisStepPublic(BaseModel):
    """执行步骤（``diagnosis_steps`` 没有 ``updated_at``）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    sequence: int
    phase: str
    status: str
    payload: dict[str, Any]
    created_at: datetime


class DiagnosisEvidencePublic(BaseModel):
    """一条证据。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    step_id: int | None
    kind: str
    source_type: str
    source_id: str
    source_title: str | None
    summary: str
    payload: dict[str, Any]
    created_at: datetime

    # 非数据库列：报告里引用它时用的编号（1 起，见模块 docstring）
    citation_no: int | None = None


class DiagnosisReportPublic(BaseModel):
    """一版诊断报告。``payload.review`` 是评审结论（报告先写、后评，所以结论是回填的）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int
    is_final: bool
    title: str | None
    summary: str | None
    risk_level: str | None
    possible_causes: list[dict[str, Any]]
    suggested_actions: list[dict[str, Any]]
    need_shutdown: str | None
    need_field_service: bool | None
    content: str
    payload: dict[str, Any]
    llm_model: str | None
    created_at: datetime


class ReportEvidenceLinkPublic(BaseModel):
    """报告 → 证据的引用边（``claim_key`` 说明这条证据撑的是哪个结论）。"""

    model_config = ConfigDict(from_attributes=True)

    report_id: int
    evidence_id: int
    citation_order: int
    claim_key: str | None


class DiagnosisDetail(BaseModel):
    """一个诊断任务的完整过程（详情/回放接口的 data）。"""

    task: DiagnosisTaskDetail
    steps: list[DiagnosisStepPublic]
    evidence: list[DiagnosisEvidencePublic]
    reports: list[DiagnosisReportPublic]
    links: list[ReportEvidenceLinkPublic]
