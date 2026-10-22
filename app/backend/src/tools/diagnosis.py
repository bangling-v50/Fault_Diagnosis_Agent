from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from app.backend.src.services.diagnosis_service import DiagnosisBrief, DiagnosisService

DIAGNOSIS_HISTORY_TOOL_NAME = "diagnosis_history"

DEFAULT_LIMIT = 3
MAX_CAUSE_CHARS = 160

# 查不到时的明确文案（同检索工具：宁可明说查不到，也别留空让模型自己发挥）
_NO_HIT_MESSAGE = (
    "没有查到诊断记录。这台设备可能还没有触发过诊断，或诊断尚未完成——"
    "请不要凭空推测诊断结论。"
)


def build_diagnosis_history_tool(
    service: DiagnosisService,
    *,
    tenant_id: int,
    device_id: int | None = None,
    device_label: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Any:
    """生成一个绑定到"某个租户（可选某台设备）"的诊断查询工具。"""

    @tool(DIAGNOSIS_HISTORY_TOOL_NAME)
    async def diagnosis_history() -> str:
        """查询最近的故障诊断结论。

        输入：无参数。
        输出：最近几次诊断的状态、告警码、风险等级、结论摘要、可能原因与建议措施。
        """
        briefs = await service.recent_diagnoses(
            tenant_id=tenant_id, device_id=device_id, limit=limit
        )
        if not briefs:
            return _NO_HIT_MESSAGE
        target = device_label or ("本租户" if device_id is None else f"设备 {device_id}")
        header = f"{target} 最近 {len(briefs)} 次故障诊断："
        blocks = [_format_brief(index, brief) for index, brief in enumerate(briefs, start=1)]
        return "\n\n".join([header, *blocks])

    return diagnosis_history


def _format_brief(index: int, brief: DiagnosisBrief) -> str:
    head = f"诊断 #{brief.task_id} ｜ {brief.status}"
    if brief.alert_code:
        head += f" ｜ 告警码 {brief.alert_code}"
    if brief.risk_level:
        head += f" ｜ 风险 {brief.risk_level}"
    lines = [f"[{index}] {head}"]
    if brief.report_title:
        lines.append(f"    结论：{brief.report_title}")
    if brief.summary:
        lines.append(f"    摘要：{brief.summary}")
    if brief.possible_causes:
        causes = "；".join(_truncate(item) for item in brief.possible_causes)
        lines.append(f"    可能原因：{causes}")
    if brief.suggested_actions:
        actions = "；".join(_truncate(item) for item in brief.suggested_actions)
        lines.append(f"    建议措施：{actions}")
    if brief.status != "SUCCEEDED":
        lines.append("    （这次诊断没有正常完成，结论仅供参考）")
    return "\n".join(lines)


# 单条原因/建议的截断长度：一条就能占满上下文的话，宁可不给全文
def _truncate(text: str, limit: int = MAX_CAUSE_CHARS) -> str:
    value = text.strip()
    return value if len(value) <= limit else f"{value[:limit]}…"
