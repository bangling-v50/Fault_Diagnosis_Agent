from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 与 DB 的 CHECK 约束一致（见 models/alert.py）
AlertLevel = Literal["info", "warning", "critical"]


class DeviceAlertRequest(BaseModel):
    """工控机上报的一条设备告警。

    两个约定：

    - ``occurred_at`` 是**设备侧时间**；服务端用它作为 ``last_seen_at``（新建时同时作为
      ``first_seen_at``），不传则用服务端当前时间。
    - **重复上报由服务端合并计数**（``occurrence_count``）：设备只管每次发生都报一次，
      服务端按「同设备 + 同告警码 + 仍未处理」合并，不要指望设备自己累计。
    """

    alert_code: str = Field(min_length=1, max_length=128, description="告警码，如 E-1024")
    alert_level: AlertLevel = Field(description="告警级别：info / warning / critical")
    alert_name: str | None = Field(default=None, max_length=255, description="告警名称")
    occurred_at: datetime | None = Field(
        default=None, description="设备侧发生时间（请带时区）"
    )
    source: str | None = Field(
        default=None, max_length=128, description="来源标识，如 '工控机/PLC-1'"
    )
    raw_message: str | None = Field(
        default=None, max_length=8000, description="原始报文，排查用（长度上限 8000）"
    )
    sensor_summary: dict[str, Any] | None = Field(
        default=None, description="测点/传感器快照，任意 JSON"
    )


class AlertPublic(BaseModel):
    """告警详情 / 列表项。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int
    device_id: int
    alert_code: str
    alert_name: str | None
    alert_level: str
    status: str
    source: str | None
    raw_message: str | None
    diagnosis_type: str | None
    diagnosis_score: float | None
    sensor_summary: dict[str, Any] | None
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    occurrence_count: int
    created_at: datetime
    updated_at: datetime


class AlertIngestResponse(BaseModel):
    """上报响应：让工控机/现场确认"收到了"，并告知是否触发了诊断。

    ``merged=True`` 表示这是同一故障的重复上报（仅累加次数），**不会**有新诊断任务；
    ``merged=False`` 才是新告警，``diagnosis_task_id`` / ``job_id`` 指向本次触发的诊断。
    """

    alert_id: int
    merged: bool
    occurrence_count: int
    diagnosis_task_id: int | None = None
    job_id: int | None = None
