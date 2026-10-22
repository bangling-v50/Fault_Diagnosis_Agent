from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.backend.src.models import UserModel
from app.backend.src.models.chat import ChatMessageModel, ChatSessionModel, UserMemoryModel
from app.backend.src.repositories.audits import ToolCallAuditRepository
from app.backend.src.repositories.chat import (
    ChatMessageRepository,
    ChatSessionRepository,
    UserMemoryRepository,
)
from app.backend.src.repositories.devices import DeviceRepository
from app.backend.src.services.chat_agent import (
    AgentContentDelta,
    AgentReasoningDelta,
    AgentToolEvent,
    AgentTurnCompleted,
    ChatAgentRunner,
    ChatTurnMessage,
)
from app.backend.src.services.chat_memory import (
    ChatContextLimitReached,
    ChatMemoryService,
    MemoryState,
)
from app.backend.src.services.chat_turn import handle_tool_event, persist_turn
from app.backend.src.services.job_service import JobService
from app.backend.src.settings import Settings
from app.backend.src.tools.chat import ChatToolDeps, build_chat_tools

logger = logging.getLogger(__name__)

# 会话默认标题的截断长度（取首条用户消息）
TITLE_MAX_CHARS = 80
# "上一个会话"要闲置多久才算结束：太短了用户可能只是切出去看一眼
PREVIOUS_SESSION_IDLE_MINUTES = 30
# 聊天工具的默认提示词
BASE_SYSTEM_PROMPT = (
    "你是工业设备售后运维的助手，面向现场操作员与售后工程师。\n"
    "你可以调用工具查询知识库、历史诊断、历史告警与维修工单——**需要事实时必须先查**，"
    "不要凭印象回答。查不到就如实说没查到，不要编造。\n"
    "回答用中文，先给结论再给依据；引用资料时说明来自哪份文档或哪次记录。"
)


class ChatError(Exception):
    """携带机器可读错误码的领域异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ChatService:
    def __init__(
        self,
        *,
        session_repo: ChatSessionRepository,
        message_repo: ChatMessageRepository,
        memory_repo: UserMemoryRepository,
        audit_repo: ToolCallAuditRepository,
        device_repo: DeviceRepository,
        memory_service: ChatMemoryService,
        runner: ChatAgentRunner,
        tool_deps: ChatToolDeps,
        job_service: JobService,
        settings: Settings,
    ) -> None:
        self._session_repo = session_repo
        self._message_repo = message_repo
        self._memory_repo = memory_repo
        self._audit_repo = audit_repo
        self._device_repo = device_repo
        self._memory_service = memory_service
        self._runner = runner
        self._tool_deps = tool_deps
        self._job_service = job_service
        self._settings = settings

    # ---------- 会话管理 ----------

    async def create_session(
        self,
        actor: UserModel,
        *,
        device_id: int | None = None,
        alert_id: int | None = None,
        diagnosis_report_id: int | None = None,
    ) -> ChatSessionModel:
        """建会话。

        **租户从设备反推，或取账号自己的**——两种账号的处境不同：

        - `operator` 绑定了租户，直接用它；带设备时还要校验设备确实属于本租户
          （请求体是客户端给的，不校验就等于允许把别家设备挂进自己的会话，然后让工具去查它）；
        - `engineer` / `admin` **没有租户**（他们跨租户），所以必须带 `device_id` 才有
          租户上下文——这也正是这类账号的用法："针对这台设备的诊断追问"。

        `chat_sessions.tenant_id` 是 NOT NULL，所以"没有租户上下文"这件事必须在这里挡住，
        而不是等数据库报错。
        """
        tenant_id = actor.tenant_id
        if device_id is not None:
            device = await self._device_repo.find_by_id(device_id)
            if device is None:
                raise ChatError("DEVICE_NOT_FOUND", "设备不存在")
            if tenant_id is not None and device.tenant_id != tenant_id:
                raise ChatError("DEVICE_NOT_FOUND", "设备不存在或不属于本租户")
            # engineer/admin 没有租户，就跟着设备走
            tenant_id = device.tenant_id
        if tenant_id is None:
            raise ChatError(
                "TENANT_REQUIRED", "请从某台设备的诊断页发起追问，或使用绑定了租户的账号"
            )
        return await self._session_repo.create(
            tenant_id=tenant_id,
            user_id=actor.id,
            thread_id=f"chat:{uuid4().hex}",
            device_id=device_id,
            alert_id=alert_id,
            diagnosis_report_id=diagnosis_report_id,
        )

    async def list_sessions(self, actor: UserModel) -> list[ChatSessionModel]:
        return await self._session_repo.list_by_user(actor.id)

    async def get_session(
        self, actor: UserModel, session_id: int, *, include_tool: bool = False
    ) -> tuple[ChatSessionModel, list[ChatMessageModel]]:
        """取会话与消息。

        **聊天按用户私有**：只有会话主人能看，engineer/admin 也不例外（已定稿的可见性规则）。
        所以这里的校验是"是不是本人的"，而不是"是不是同租户的"。
        """
        session = await self._require_own_session(actor, session_id)
        messages = await self._message_repo.list_by_session(
            session_id, include_tool=include_tool
        )
        return session, messages

    async def delete_session(self, actor: UserModel, session_id: int) -> None:
        await self._require_own_session(actor, session_id)
        await self._session_repo.delete(session_id)

    async def memory_state(self, session: ChatSessionModel) -> MemoryState:
        """上下文占用状态。**是 async 的**——因为另一套实现（LangGraph 版）要从 checkpointer
        现算，路由那边统一 `await`，两版签名保持一致。"""
        return self._memory_service.usage_state(session)

    # ---------- 长期记忆（用户自己的） ----------

    async def list_memories(self, actor: UserModel) -> list[UserMemoryModel]:
        return await self._memory_repo.list_all(actor.id)

    async def delete_memory(self, actor: UserModel, memory_id: str) -> None:
        """删除一条长期记忆。**只能删自己的**——记忆是用户级资产。

        本版的主键是自增数字，而另一套实现（Store 版）用的是内容指纹字符串，
        所以路由把它当字符串传进来、这里再收窄。
        """
        try:
            numeric_id = int(memory_id)
        except (TypeError, ValueError):
            raise ChatError("MEMORY_NOT_FOUND", "记忆不存在") from None
        memory = await self._memory_repo.find_by_id(numeric_id)
        if memory is None or memory.user_id != actor.id:
            raise ChatError("MEMORY_NOT_FOUND", "记忆不存在")
        await self._memory_repo.delete(numeric_id)

    async def extract_memories_for_session(self, actor: UserModel, session_id: int) -> int:
        """手动触发一次提炼（会话页的「结束会话并记住重点」走这里）。"""
        session = await self._require_own_session(actor, session_id)
        result = await self._memory_service.extract_memories(session)
        return result.written

    async def extract_memories_by_id(self, session_id: int) -> int:
        """**后台任务入口**：由 job 触发，没有 actor 上下文。

        会话不存在就静默返回 0——job 重试时前一次可能已经成功删过会话了，
        为这个报错没有意义。
        """
        session = await self._session_repo.find_by_id(session_id)
        if session is None:
            return 0
        result = await self._memory_service.extract_memories(session)
        return result.written

    # ---------- 对话（SSE 事件源） ----------

    async def stream_message(
        self, actor: UserModel, session: ChatSessionModel, content: str
    ) -> AsyncIterator[dict]:
        """跑一轮对话，逐条吐出 SSE 事件字典（路由负责编码成帧）。"""
        text = (content or "").strip()
        if not text:
            yield _event("error", {"code": "EMPTY_MESSAGE", "message": "消息不能为空"})
            return
        if session.user_id != actor.id:
            yield _event("error", {"code": "FORBIDDEN", "message": "无权在此会话中发言"})
            return

        base_prompt = await self._build_base_prompt(session)
        # ① 预算判定与压缩（**在写库之前**；超限就抛，用户消息不会进库）
        try:
            session = await self._memory_service.prepare(session, system_prompt=base_prompt)
        except ChatContextLimitReached as exc:
            yield _event("error", {"code": "CHAT_CONTEXT_LIMIT_REACHED", "message": str(exc)})
            return

        is_first_message = (await self._message_repo.count(session.id)) == 0

        # ② 落库用户消息（先落，这样即使回答失败提问也留得住）
        await self._message_repo.append(session_id=session.id, role="user", content=text)
        if is_first_message:
            await self._session_repo.update_title(session.id, text[:TITLE_MAX_CHARS])
            # ③ 新会话的第一句 → 后台把"上一个还没提炼的会话"提炼掉（短会话唯一的兜底路径）
            await self._enqueue_previous_extraction(session)

        history = await self._build_history(session)
        tools = build_chat_tools(
            self._tool_deps,
            tenant_id=session.tenant_id,
            device_id=session.device_id,
            device_label=await self._device_label(session),
        )
        system_prompt = self._memory_service.prompt_with_memory(base_prompt, session.memory_summary)

        audits: dict[str, int] = {}  # run_id -> audit 行 id（只在本次流里有意义）
        completed: AgentTurnCompleted | None = None
        sequence = 0

        try:
            async for item in self._runner.stream(
                system_prompt=system_prompt, history=history, tools=tools
            ):
                if isinstance(item, AgentContentDelta):
                    sequence += 1
                    yield _event("content.delta", {"delta": item.text, "sequence": sequence})
                elif isinstance(item, AgentReasoningDelta):
                    sequence += 1
                    yield _event("reasoning.delta", {"delta": item.text, "sequence": sequence})
                elif isinstance(item, AgentToolEvent):
                    payload = await handle_tool_event(
                        audit_repo=self._audit_repo,
                        tenant_id=session.tenant_id,
                        session_id=session.id,
                        item=item,
                        audits=audits,
                    )
                    yield _event("tool.call", {"toolCall": payload})
                elif isinstance(item, AgentTurnCompleted):
                    completed = item
        except Exception as exc:  # noqa: BLE001 —— Agent 内部的任何异常都不该 500，转成事件
            logger.exception("聊天 Agent 执行失败 session=%s", session.id)
            yield _event("error", {"code": "CHAT_AGENT_FAILED", "message": str(exc)[:500]})
            return

        # ④ 落库助手与工具消息（数据来自最终 state，不是边流边拼的近似品）
        if completed is not None:
            await persist_turn(
                message_repo=self._message_repo,
                audit_repo=self._audit_repo,
                session_id=session.id,
                messages=completed.messages,
                audits=audits,
            )
        session = await self._memory_service.refresh_usage(session, system_prompt=base_prompt)
        yield _event(
            "complete",
            {
                "result": {
                    "sessionId": session.id,
                    "messageCount": await self._message_repo.count(session.id),
                    "memory": _memory_payload(self._memory_service.usage_state(session)),
                }
            },
        )

    # ---------- 内部 ----------

    # 下面两件事（归档落库 / 工具审计）与"历史怎么管"无关，两套实现共用同一份代码——
    # 见 `services/chat_turn.py`。所以这里没有 _persist_turn / _handle_tool_event 了。


    async def _build_history(self, session: ChatSessionModel) -> list[ChatTurnMessage]:
        """重建喂给模型的对话（**含 tool 轮次**）。

        这是"加 tool 角色"这件事的落点：``assistant(tool_calls) → tool(结果)`` 原样还原，
        模型才答得上"你刚才查到的那个工单里方案是什么"。
        """
        rows = await self._message_repo.list_for_model(
            session.id, offset=session.compacted_message_count
        )
        return [
            ChatTurnMessage(
                role=row.role,
                content=row.content,
                tool_calls=list((row.metadata_json or {}).get("toolCalls") or []),
                tool_call_id=(row.metadata_json or {}).get("toolCallId"),
            )
            for row in rows
            if row.role in ("user", "assistant", "tool")
        ]

    async def _build_base_prompt(self, session: ChatSessionModel) -> str:
        """基础提示词 = 角色说明 + 长期记忆 + 会话上下文（不含会话摘要，那个由记忆服务拼）。"""
        parts = [BASE_SYSTEM_PROMPT]

        memories = await self._memory_repo.list_active(session.user_id, limit=20)
        if memories:
            lines = "\n".join(f"- {item.content}" for item in memories)
            parts.append(f"## 关于这位用户的长期记忆\n{lines}")

        context = await self._session_context(session)
        if context:
            parts.append(context)
        return "\n\n".join(parts)

    async def _session_context(self, session: ChatSessionModel) -> str:
        """会话绑定的设备/告警/诊断报告——从诊断页「追问」进来时才有。"""
        if session.device_id is None:
            return ""
        device = await self._device_repo.find_by_id(session.device_id)
        if device is None:
            return ""
        lines = [
            f"- 设备：{device.device_code}"
            + (f"（{device.device_name}）" if device.device_name else ""),
            f"- 机型：{device.device_model or '未填'}",
        ]
        if session.alert_id:
            lines.append(f"- 关联告警：#{session.alert_id}")
        if session.diagnosis_report_id:
            lines.append(f"- 来源诊断报告：#{session.diagnosis_report_id}")
        return "## 本次会话的设备上下文\n" + "\n".join(lines) + "\n（默认围绕这台设备回答）"

    async def _device_label(self, session: ChatSessionModel) -> str | None:
        if session.device_id is None:
            return None
        device = await self._device_repo.find_by_id(session.device_id)
        return device.device_code if device else None

    async def _enqueue_previous_extraction(self, session: ChatSessionModel) -> None:
        """把"上一个已闲置、还没提炼过"的会话排进后台提炼。

        这是**短会话唯一的兜底路径**：压缩要聊够几十轮才触发，而很多会话三轮就结束了——
        用户开新会话时顺手把上一段提炼掉，既不依赖用户点按钮，也不需要定时器。
        """
        idle_before = datetime.now(timezone.utc) - timedelta(minutes=PREVIOUS_SESSION_IDLE_MINUTES)
        previous = await self._session_repo.find_extractable(
            session.user_id, idle_before=idle_before
        )
        if previous is None:
            return
        try:
            await self._job_service.enqueue(
                kind="chat_memory_extract",
                resource_type="chat_session",
                resource_id=previous.id,
                tenant_id=previous.tenant_id,
                payload={"sessionId": previous.id},
                max_attempts=2,
            )
        except Exception:  # noqa: BLE001 —— 提炼排不上队不该影响用户这次对话
            logger.warning("上一个会话的长期记忆提炼入队失败 session=%s", previous.id)

    async def _require_own_session(self, actor: UserModel, session_id: int) -> ChatSessionModel:
        session = await self._session_repo.find_by_id(session_id)
        # 不区分"不存在"和"不是你的"——避免被当成探测接口（同设备令牌那套思路）
        if session is None or session.user_id != actor.id:
            raise ChatError("CHAT_SESSION_NOT_FOUND", "会话不存在")
        return session


# 工具输出在 SSE 载荷里的截断长度（前端只用来显示"查到了什么"，不要全文）
MAX_TOOL_OUTPUT_CHARS = 600
# 审计里存的结果摘要长度
AUDIT_SUMMARY_CHARS = 2000


def _event(event_type: str, payload: dict) -> dict:
    """统一的 SSE 事件信封（照 src1）：id / type / channel / timestamp + 业务载荷。"""
    return {
        "id": f"evt_{uuid4().hex}",
        "type": event_type,
        "channel": "chat",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **payload,
    }


def _memory_payload(state: MemoryState) -> dict:
    return {
        "mode": state.memory_mode,
        "contextTokens": state.context_tokens,
        "contextWindowTokens": state.context_window_tokens,
        "usagePercent": state.usage_percent,
        "compactedMessageCount": state.compacted_message_count,
        "lastCompactedAt": state.last_compacted_at,
    }
