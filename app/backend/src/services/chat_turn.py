from __future__ import annotations

import logging
from collections.abc import Sequence

from app.backend.src.repositories.audits import ToolCallAuditRepository
from app.backend.src.repositories.chat import ChatMessageRepository
from app.backend.src.services.chat_agent import AgentToolEvent, ChatTurnMessage

logger = logging.getLogger(__name__)

# 工具输出在 SSE 载荷里的截断长度（前端只用来显示"查到了什么"，不要全文）
MAX_TOOL_OUTPUT_CHARS = 600
# 审计里存的结果摘要长度
AUDIT_SUMMARY_CHARS = 2000


async def persist_turn(
    *,
    message_repo: ChatMessageRepository,
    audit_repo: ToolCallAuditRepository,
    session_id: int,
    messages: Sequence[ChatTurnMessage],
    audits: dict[str, int],
) -> None:
    """把本轮新增的消息写进归档表（``chat_messages``）。

    - ``assistant`` 带 ``tool_calls`` → 工具调用信息存进 ``metadata``（表里没有专门的列）；
    - ``tool`` → 内容就是工具结果，``tool_call_id`` 存 metadata（**回放时还原配对全靠它**）；
    - 顺带把审计行挂到对应的 tool 消息上（``chat_message_id``）。

    ⚠️ 两个实现里这些消息的角色不同：手动版里它**参与上下文**，框架版里它**只用于展示**
    （上下文由 checkpointer 管）。所以两边行为一致，但**意义不同**——这点在看日志时容易搞混。
    """
    pending_tools = list(audits.items())  # [(run_id, audit_id)]，按开始顺序
    tool_index = 0
    for message in messages:
        metadata: dict = {}
        if message.role == "assistant" and message.tool_calls:
            metadata["toolCalls"] = message.tool_calls
        if message.role == "tool":
            metadata["toolCallId"] = message.tool_call_id
            # 审计与 tool 消息按**出现顺序**配对：审计是 started 时建的，顺序一致
            if tool_index < len(pending_tools):
                metadata["auditId"] = pending_tools[tool_index][1]
                tool_index += 1
        row = await message_repo.append(
            session_id=session_id,
            role=message.role,
            content=message.content or "",
            metadata=metadata or None,
        )
        if message.role == "tool" and metadata.get("auditId"):
            await audit_repo.attach_message(int(metadata["auditId"]), chat_message_id=row.id)


async def handle_tool_event(
    *,
    audit_repo: ToolCallAuditRepository,
    tenant_id: int,
    session_id: int,
    item: AgentToolEvent,
    audits: dict[str, int],
) -> dict:
    """工具事件 → 审计行 + 给前端的状态载荷。

    审计是**尽力而为**：它挂了也不能影响对话，所以整段包在 try 里（src1 同样的取舍）。
    """
    payload: dict = {"id": item.run_id, "name": item.name, "status": item.status}
    if item.arguments:
        payload["input"] = item.arguments
    if item.output:
        payload["output"] = item.output[:MAX_TOOL_OUTPUT_CHARS]
    if item.error:
        payload["error"] = item.error
    try:
        if item.status == "started":
            audit = await audit_repo.create(
                tenant_id=tenant_id,
                session_id=session_id,
                tool_name=item.name,
                arguments=item.arguments,
            )
            audits[item.run_id] = audit.id
        else:
            audit_id = audits.get(item.run_id)
            if audit_id is not None:
                await audit_repo.finalize(
                    audit_id,
                    status="SUCCEEDED" if item.status == "completed" else "FAILED",
                    result_summary=(item.output or "")[:AUDIT_SUMMARY_CHARS],
                    error_message=item.error,
                )
    except Exception:  # noqa: BLE001
        logger.warning("写工具审计失败（不影响对话）session=%s tool=%s", session_id, item.name)
    return payload
