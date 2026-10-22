from __future__ import annotations

from operator import add
from typing import Annotated, Any, TypedDict


class DiagnosisState(TypedDict, total=False):
    # ---------- 身份与输入（runner 进图前填好，节点只读） ----------
    task_id: int
    tenant_id: int
    device_id: int
    # 触发时的**快照**（取自 diagnosis_tasks.input_payload）——执行期间源数据被改也不影响本次诊断
    alert: dict[str, Any]
    device: dict[str, Any]

    # ---------- 证据采集 ----------
    # 每条形如：{"no": 1, "evidence_id": 12, "kind": "knowledge",
    #            "kind_label": "知识库片段", "source_title": "...", "summary": "..."}
    # ``no`` 是**给模型看的引用编号**（跨三个工具从 1 起连续），报告里的 evidenceIndexes 指的就是它
    evidence_index: Annotated[list[dict[str, Any]], add]
    # 采集过程中的异常说明（如"知识库检索失败：超时"），会原样进报告提示词，逼模型如实交代
    collection_notes: Annotated[list[str], add]

    # ---------- 报告与评估 ----------
    report_draft: dict[str, Any] | None
    report_id: int
    report_version: int
    evaluation: dict[str, Any] | None
    # 历次被否的原因：``{"version": 1, "issues": [...], "source": "hard|llm"}``
    rejections: Annotated[list[dict[str, Any]], add]

    # ---------- 事件流 ----------
    events: Annotated[list[dict[str, Any]], add]
