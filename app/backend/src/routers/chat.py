from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.backend.src.containers import Container
from app.backend.src.models import UserModel
from app.backend.src.models.chat import ChatMessageModel, ChatSessionModel
from app.backend.src.schemas.chat import (
    ChatMemoryExtractResult,
    ChatMemoryPublic,
    ChatMessagePublic,
    ChatMessageRequest,
    ChatSessionCreateRequest,
    ChatSessionDetail,
    ChatSessionPublic,
)
from app.backend.src.schemas.common import APIResponse, ok
from app.backend.src.services.chat_service import ChatError
from app.backend.src.utils.auth import get_container, get_current_user
from app.backend.src.utils.errors import to_http

router = APIRouter(prefix="/chat", tags=["chat"])

_AnyUser = Annotated[UserModel, Depends(get_current_user)]


def _message_public(row: ChatMessageModel) -> ChatMessagePublic:
    """ORM → 响应体。

    手动构造而不是 ``model_validate``：模型上的属性叫 ``metadata_json``（``metadata`` 是
    SQLAlchemy 的保留字），而对外字段想叫 ``metadata``。
    """
    return ChatMessagePublic(
        id=row.id,
        role=row.role,
        content=row.content,
        metadata=row.metadata_json or {},
        created_at=row.created_at,
    )


async def _detail(
    container: Container, session: ChatSessionModel, messages: list[ChatMessageModel]
) -> ChatSessionDetail:
    return ChatSessionDetail(
        session=ChatSessionPublic.model_validate(session),
        messages=[_message_public(row) for row in messages],
        memory=await _memory_payload(container, session),
    )


async def _memory_payload(container: Container, session: ChatSessionModel) -> dict:
    # 两套实现的 memory_state 都是 async（LangGraph 版要从 checkpointer 现算）
    state = await container.chat_service.memory_state(session)
    return {
        "mode": state.memory_mode,
        "contextTokens": state.context_tokens,
        "contextWindowTokens": state.context_window_tokens,
        "usagePercent": state.usage_percent,
        "compactedMessageCount": state.compacted_message_count,
        "lastCompactedAt": state.last_compacted_at,
    }


# ---------------- 会话 ----------------


@router.post("/sessions", response_model=APIResponse[ChatSessionPublic], status_code=201)
async def create_session(
    body: ChatSessionCreateRequest,
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """建会话。带上 `device_id` 就是"针对这台设备的追问"（工具集也会随之变化）。"""
    try:
        session = await container.chat_service.create_session(
            actor,
            device_id=body.device_id,
            alert_id=body.alert_id,
            diagnosis_report_id=body.diagnosis_report_id,
        )
    except ChatError as exc:
        raise to_http(exc) from exc
    return ok(ChatSessionPublic.model_validate(session), message="会话已创建")


@router.get("/sessions", response_model=APIResponse[list[ChatSessionPublic]])
async def list_sessions(
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """我的会话列表（最近活跃的在前）。"""
    sessions = await container.chat_service.list_sessions(actor)
    return ok([ChatSessionPublic.model_validate(row) for row in sessions])


@router.get("/sessions/{session_id}", response_model=APIResponse[ChatSessionDetail])
async def get_session(
    session_id: int,
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
    include_tool: bool = Query(
        default=False, description="是否连同 role='tool' 的消息一起返回（回放/调试用）"
    ),
) -> APIResponse:
    """会话详情 + 消息 + 压缩状态。"""
    try:
        session, messages = await container.chat_service.get_session(
            actor, session_id, include_tool=include_tool
        )
    except ChatError as exc:
        raise to_http(exc) from exc
    return ok(await _detail(container, session, messages))


@router.delete("/sessions/{session_id}", response_model=APIResponse[None])
async def delete_session(
    session_id: int,
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """删会话（消息一并删；已提炼的长期记忆留着）。"""
    try:
        await container.chat_service.delete_session(actor, session_id)
    except ChatError as exc:
        raise to_http(exc) from exc
    return ok(None, message="会话已删除")


# ---------------- 对话（SSE） ----------------

@router.post("/sessions/{session_id}/messages:stream")
async def stream_message(
    session_id: int,
    body: ChatMessageRequest,
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
) -> StreamingResponse:
    """发一条消息，**流式**返回这一轮（SSE）。

    事件类型：``content.delta``（正文增量）/ ``reasoning.delta``（思维链增量）/
    ``tool.call``（工具开始与结束）/ ``error`` / ``complete``。
    """
    try:
        session, _ = await container.chat_service.get_session(actor, session_id)
    except ChatError as exc:
        raise to_http(exc) from exc

    async def event_stream():
        async for event in container.chat_service.stream_message(actor, session, body.content):
            yield _encode_sse(event)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 关掉中间层缓冲，否则"流式"会被攒成一坨再吐（nginx 之类的默认行为）
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/sessions/{session_id}/memory:extract", response_model=APIResponse[ChatMemoryExtractResult]
)
async def extract_memories(
    session_id: int,
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """手动提炼这个会话里的长期记忆（会话页的「结束会话并记住重点」走这里）。

    短会话不会触发压缩，所以这条路径是"聊了三轮就走"的场景唯一的补救手段之一
    （另一个是开新会话时后台自动提炼上一个）。
    """
    try:
        written = await container.chat_service.extract_memories_for_session(actor, session_id)
    except ChatError as exc:
        raise to_http(exc) from exc
    return ok(ChatMemoryExtractResult(written=written), message="已提炼")


# ---------------- 长期记忆 ----------------


@router.get("/memories", response_model=APIResponse[list[ChatMemoryPublic]])
async def list_memories(
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """我的长期记忆（含已作废的，便于用户看清来龙去脉）。"""
    rows = await container.chat_service.list_memories(actor)
    return ok([ChatMemoryPublic.model_validate(row) for row in rows])


@router.delete("/memories/{memory_id}", response_model=APIResponse[None])
async def delete_memory(
    # ⚠️ 是 str 不是 int：手动版的主键是自增数字，LangGraph Store 版的主键是内容指纹字符串，
    # 两版共用这一个端点，所以放宽成字符串（前端拿到的 id 原样回传，不受影响）
    memory_id: str,
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """删掉一条长期记忆。**用户必须能真的删掉**——这是 ToB 场景的合规底线。"""
    try:
        await container.chat_service.delete_memory(actor, memory_id)
    except ChatError as exc:
        raise to_http(exc) from exc
    return ok(None, message="记忆已删除")


def _encode_sse(event: dict) -> str:
    """事件字典 → SSE 帧。

    同时写 ``event:`` 与 ``data:`` 两行：前者让前端可以只按事件名分流，后者是完整载荷。
    """
    payload = json.dumps(event, ensure_ascii=False)
    return f"event: {event.get('type', 'message')}\ndata: {payload}\n\n"
