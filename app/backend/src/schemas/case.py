from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CasePublic(BaseModel):
    """一条沉淀案例。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int | None
    scope: str
    device_model: str
    alert_code: str | None
    source_work_order_id: int
    source_task_id: int | None
    source_report_id: int | None
    root_cause: str
    solution: str
    resolution_result: str
    summary: str
    keywords: list[str]
    case_fingerprint: str
    # 回流出来的知识文档与它的索引任务（可为空：建案例与建文档是两步，见 case_service）
    document_id: int | None
    index_task_id: int | None
    confirmed_by: int
    confirmed_at: datetime
    created_at: datetime


class CaseCreateRequest(BaseModel):
    """沉淀请求。四个文本字段都会进案例正文与知识文档，所以都要求非空。"""

    work_order_id: int = Field(description="来源工单（必须已结案）")
    root_cause: str = Field(min_length=1, description="确认根因")
    solution: str = Field(min_length=1, description="处理方案")
    resolution_result: str = Field(min_length=1, description="处理结果")
    summary: str | None = Field(default=None, description="不填则由标题与根因拼一句")
    keywords: list[str] | None = Field(default=None, description="不填则自动抽取")
    scope: Literal["tenant", "global"] = Field(
        default="tenant", description="tenant=本租户可见；global=所有租户共享"
    )


class CaseCreateResult(BaseModel):
    """沉淀结果。

    ``created=False`` 表示**幂等命中**：这张工单已经沉淀过，或同样内容（同机型同告警码同根因同方案）
    的案例已存在，直接返回了已有那条——重复点「沉淀为案例」不会产生第二份。
    """

    case: CasePublic
    created: bool
