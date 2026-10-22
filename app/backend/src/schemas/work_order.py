from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 与 DB 的 CHECK 一致（见 models/work_orders.py）
WorkOrderPriority = Literal["low", "medium", "high", "urgent"]
WorkOrderStatus = Literal["OPEN", "ASSIGNED", "IN_PROGRESS", "RESOLVED", "CLOSED"]


class WorkOrderPublic(BaseModel):
    """工单（列表项 / 详情）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int
    device_id: int
    alert_id: int | None
    diagnosis_report_id: int | None
    title: str
    description: str | None
    priority: str
    status: str
    assigned_to: int | None
    created_by: int | None
    confirmed_root_cause: str | None
    solution: str | None
    resolution_result: str | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    # 由列表查询 join 出来、方便列表页直接显示（不是 work_orders 的列）
    device_code: str | None = None
    device_name: str | None = None
    assigned_to_name: str | None = None


class WorkOrderCreateRequest(BaseModel):
    """建单请求。

    ``alert_id`` 强烈建议带上：它是工单与"故障码"之间唯一的纽带（见
    ``repositories/work_orders.py`` 的模块 docstring）。诊断详情页的「生成工单」会自动带，
    手工建单时页面上也有一个可选的告警下拉。
    """

    device_id: int = Field(description="工单属于哪台设备（租户由它反推，不用传）")
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    priority: WorkOrderPriority = "medium"
    alert_id: int | None = Field(default=None, description="关联的告警（必须属于同一台设备）")
    diagnosis_report_id: int | None = Field(default=None, description="来源诊断报告")
    assigned_to: int | None = Field(default=None, description="处理人（engineer/admin）")


class WorkOrderUpdateRequest(BaseModel):
    """局部更新：只提交要改的字段。

    路由用 ``model_dump(exclude_unset=True)`` 取值——**没传的字段与显式传 null 是两回事**，
    后者用来"取消指派"（``assigned_to: null``）。
    """

    status: WorkOrderStatus | None = None
    priority: WorkOrderPriority | None = None
    assigned_to: int | None = None
    description: str | None = None
    confirmed_root_cause: str | None = Field(default=None, description="结案必填")
    solution: str | None = Field(default=None, description="结案必填")
    resolution_result: str | None = None
