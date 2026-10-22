from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatSessionPublic(BaseModel):
    """一个聊天会话。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int
    user_id: int
    title: str | None
    # 会话上下文：从诊断页「追问」进来时才有
    device_id: int | None
    alert_id: int | None
    diagnosis_report_id: int | None
    created_at: datetime
    updated_at: datetime


class ChatMessagePublic(BaseModel):
    """一条聊天消息。

    ``metadata`` 里可能带 ``toolCalls``（助手发起的工具调用）、``toolCallId`` / ``auditId``
    （工具结果那条），前端据此展示"这次回答查了什么"。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str  # user | assistant | system | tool
    content: str
    metadata: dict[str, Any]
    created_at: datetime


class ChatMemoryPublic(BaseModel):
    """一条长期记忆。

    ``id`` 放宽成 ``int | str``：手动版是自增主键，LangGraph Store 版是内容指纹字符串——
    两版共用同一个端点，前端把它当不透明句柄原样回传即可。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int | str
    kind: str  # preference | fact | constraint
    content: str
    source_session_id: int | None
    status: str  # active | archived
    created_at: datetime
    updated_at: datetime


class ChatSessionDetail(BaseModel):
    """会话详情：会话本体 + 消息 + 压缩状态（前端那个上下文占用条靠它）。"""

    session: ChatSessionPublic
    messages: list[ChatMessagePublic]
    memory: dict[str, Any]


class ChatSessionCreateRequest(BaseModel):
    """建会话。三个 id 都是可选的——从诊断详情页点「追问」时会把它们带上。"""

    device_id: int | None = None
    alert_id: int | None = None
    diagnosis_report_id: int | None = None


class ChatMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000, description="用户这一轮说的话")


class ChatMemoryExtractResult(BaseModel):
    """一次记忆提炼的结果。"""

    written: int = Field(description="本次新写入的长期记忆条数")
