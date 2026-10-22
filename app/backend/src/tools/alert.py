from __future__ import annotations

from datetime import datetime
from typing import Any

from langchain_core.tools import tool

from app.backend.src.models.alert import AlertModel
from app.backend.src.services.alert_service import AlertService

ALERT_HISTORY_TOOL_NAME = "alert_history"

DEFAULT_LIMIT = 5

LEVEL_LABELS = {"info": "提示", "warning": "警告", "critical": "严重"}
STATUS_LABELS = {"NEW": "新告警（未处理）", "DIAGNOSED": "已诊断"}

_NO_HIT_MESSAGE = "没有查到告警记录。这台设备可能还没上报过告警，请不要凭空推测。"


def build_alert_history_tool(
    service: AlertService,
    *,
    tenant_id: int,
    device_id: int | None = None,
    device_label: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Any:
    """生成一个绑定到"某个租户（可选某台设备）"的告警查询工具。"""

    @tool(ALERT_HISTORY_TOOL_NAME)
    async def alert_history() -> str:
        """查询最近的设备告警。

        输入：无参数。
        输出：最近若干条告警的告警码、名称、级别、状态、累计出现次数与最近发生时间。
        """
        rows = await service.recent_alerts(
            tenant_id=tenant_id, device_id=device_id, limit=limit
        )
        if not rows:
            return _NO_HIT_MESSAGE
        target = device_label or ("本租户" if device_id is None else f"设备 {device_id}")
        header = f"{target} 最近的告警（共 {len(rows)} 条）："
        blocks = [_format_alert(index, row) for index, row in enumerate(rows, start=1)]
        return "\n\n".join([header, *blocks])

    return alert_history


def _format_alert(index: int, row: AlertModel) -> str:
    level = LEVEL_LABELS.get(row.alert_level, row.alert_level)
    status = STATUS_LABELS.get(row.status, row.status)
    parts = [
        f"[{index}] {row.alert_code}",
        row.alert_name or "未命名",
        f"级别 {level}",
        f"状态 {status}",
    ]
    if row.occurrence_count > 1:
        # "累计出现 N 次"是判断"老毛病还是新问题"的关键信息，值得单独说
        parts.append(f"累计出现 {row.occurrence_count} 次")
    lines = [" ｜ ".join(parts)]
    last_seen = _fmt_time(row.last_seen_at)
    if last_seen:
        lines.append(f"    最近发生：{last_seen}")
    if row.raw_message:
        lines.append(f"    原始报文：{row.raw_message[:200]}")
    return "\n".join(lines)


def _fmt_time(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M") if value else None
