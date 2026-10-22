from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from app.backend.src.services.work_order_service import (
    WorkOrderHistoryItem,
    WorkOrderService,
)

WORK_ORDER_HISTORY_TOOL_NAME = "work_order_history"

DEFAULT_LIMIT = 5

# 查不到时的明确文案（和检索工具一个套路：宁可明说查不到，也不留空让模型自己发挥）
_NO_HIT_MESSAGE = (
    "没有查到该设备同一告警码的历史维修工单——这类故障很可能是首次发生，"
    "请以知识库手册和现场数据为准，不要凭猜测编造维修记录。"
)
# 闭包没给默认码、模型也没传码时的兜底（正常链路不该走到这里）
_MISSING_CODE_MESSAGE = "未指定告警码，无法查询历史维修工单。请提供该故障的告警码。"

# 数据库里存的是英文状态值，这里译一遍只是为了提示词好读（原值一并保留，便于溯源）
_STATUS_LABELS = {
    "OPEN": "待受理",
    "ASSIGNED": "已派单",
    "IN_PROGRESS": "处理中",
    "RESOLVED": "已解决",
    "CLOSED": "已关闭",
}


def build_work_order_history_tool(
    service: WorkOrderService,
    *,
    device_id: int,
    default_alert_code: str | None = None,
    device_label: str | None = None,
    tenant_id: int | None = None,
    exclude_alert_id: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Any:
    """生成一个绑定到"某台设备 + 某个默认告警码"的工单查询工具。

    按需生成（每个诊断任务 / 每个聊天会话各一份），**不要做成全局单例**——闭包里装着
    属于某次任务的数据，正是"单例里禁止保存请求/任务状态"那条规矩的反面用法。

    ``exclude_alert_id`` 传当前告警的 id，可排掉"本次故障自己产生的那张工单"。
    """

    @tool(WORK_ORDER_HISTORY_TOOL_NAME)
    async def work_order_history(alert_code: str = "") -> str:
        """查询本设备历史上针对同一告警码的维修工单，看这类故障以前是怎么修的。

        输入：``alert_code`` 告警码（如 "E-1024"）。**通常留空**，留空即为当前故障的告警码。
        输出：历史工单的维修记录——故障现象、确认的根因、处理方案、处理结果；已结案的排在前面。
        """
        code = alert_code.strip() or (default_alert_code or "").strip()
        if not code:
            return _MISSING_CODE_MESSAGE

        items = await service.history_by_code(
            device_id=device_id,
            alert_code=code,
            limit=limit,
            tenant_id=tenant_id,
            exclude_alert_id=exclude_alert_id,
        )
        if not items:
            return _NO_HIT_MESSAGE

        target = device_label or f"设备 {device_id}"
        header = f"{target} 上「{code}」的历史维修工单共 {len(items)} 条（已结案的排在前面）："
        blocks = [_format_item(index, item) for index, item in enumerate(items, start=1)]
        return "\n\n".join([header, *blocks])

    return work_order_history


# 一条工单渲染成一小段文本；只打印有值的字段，避免满屏"无/None"干扰模型
def _format_item(index: int, item: WorkOrderHistoryItem) -> str:
    status = _STATUS_LABELS.get(item.status, item.status)
    head = f"工单 #{item.work_order_id} ｜ {status}({item.status}) ｜ 优先级 {item.priority}"
    lines = [
        f"[{index}] {head}",
        f"    标题：{item.title}",
    ]
    timeline = _join_parts(
        ("开单", _readable_time(item.created_at)),
        ("关单", _readable_time(item.closed_at)),
    )
    if timeline:
        lines.append(f"    {timeline}")
    if item.description:
        lines.append(f"    现象：{item.description}")
    if item.confirmed_root_cause:
        lines.append(f"    确认根因：{item.confirmed_root_cause}")
    if item.solution:
        lines.append(f"    处理方案：{item.solution}")
    if item.resolution_result:
        lines.append(f"    处理结果：{item.resolution_result}")
    # 没结案的工单不可能有结论，明说一句，免得模型把这行空白当成"修了但没记录"
    if not item.closed and not any(
        (item.confirmed_root_cause, item.solution, item.resolution_result)
    ):
        lines.append("    （该工单尚未结案，暂无维修结论）")
    return "\n".join(lines)


# 把 ISO 字符串渲染成"2026-03-02 10:00"这种好读的样子；存进证据的仍是完整 ISO（见 service）
def _readable_time(value: str | None) -> str | None:
    if not value:
        return None
    return value[:16].replace("T", " ")


# 把 (标签, 值) 里"有值"的那些拼成 "开单：... ｜ 关单：..."；全为空则返回空串
def _join_parts(*pairs: tuple[str, str | None]) -> str:
    return " ｜ ".join(f"{label}：{value}" for label, value in pairs if value)
