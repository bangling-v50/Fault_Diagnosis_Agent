from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.backend.src.services.alert_service import AlertService
from app.backend.src.services.diagnosis_service import DiagnosisService
from app.backend.src.services.retrieval_service import RetrievalService
from app.backend.src.services.work_order_service import WorkOrderService
from app.backend.src.tools.alert import build_alert_history_tool
from app.backend.src.tools.diagnosis import build_diagnosis_history_tool
from app.backend.src.tools.knowledge import build_knowledge_retrieval_tool
from app.backend.src.tools.work_order import build_work_order_history_tool


@dataclass(frozen=True, slots=True)
class ChatToolDeps:
    """建工具要用到的服务（容器装配时塞进来，全是无状态单例）。"""

    retrieval_service: RetrievalService
    work_order_service: WorkOrderService
    diagnosis_service: DiagnosisService
    alert_service: AlertService


def build_chat_tools(
    deps: ChatToolDeps,
    *,
    tenant_id: int,
    device_id: int | None = None,
    device_label: str | None = None,
) -> list[Any]:
    """按会话上下文组装工具集。

    前三个工具**总是挂**（都能只按租户工作）：

    - ``knowledge_retrieval``：查手册/SOP/案例，问题可以是任意自由文本；
    - ``diagnosis_history``：这台设备（或本租户）以前诊断过几次、结论是什么；
    - ``alert_history``：最近报过哪些警。

    只有 ``work_order_history`` **绑死一台设备**——工单与故障码的联系是
    "设备 + 告警码"，没有设备就没有查询的起点，所以会话不带设备时**不挂它**，
    而不是把设备 id 交给模型去填。
    """
    tools: list[Any] = [
        build_knowledge_retrieval_tool(deps.retrieval_service, tenant_id=tenant_id),
        build_diagnosis_history_tool(
            deps.diagnosis_service,
            tenant_id=tenant_id,
            device_id=device_id,
            device_label=device_label,
        ),
        build_alert_history_tool(
            deps.alert_service,
            tenant_id=tenant_id,
            device_id=device_id,
            device_label=device_label
        ),
    ]
    if device_id is not None:
        tools.append(
            build_work_order_history_tool(
                deps.work_order_service,
                device_id=device_id,
                device_label=device_label
            )
        )
    return tools
