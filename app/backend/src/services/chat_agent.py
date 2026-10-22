from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.backend.src.llm_provider.chat import ChatModelProvider, message_text

# 只有这几种角色会进上下文；system 由提示词单独给，不走这里
CONTEXT_ROLES = ("user", "assistant", "tool")


@dataclass(frozen=True, slots=True)
class ChatTurnMessage:
    """一条进上下文/要落库的消息（**我们自己的类型，不是 LangChain 的**）。

    ``tool_calls`` 只在 ``role='assistant'`` 且有工具调用时有值；``tool_call_id`` 只在
    ``role='tool'`` 时有值——这两个字段就是"工具轮次能不能原样还原"的关键。
    """

    role: str
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None


# ---------- runner 吐出来的事件（服务层消费这些，不碰 LangChain） ----------


@dataclass(frozen=True, slots=True)
class AgentContentDelta:
    text: str


@dataclass(frozen=True, slots=True)
class AgentReasoningDelta:
    text: str


@dataclass(frozen=True, slots=True)
class AgentToolEvent:
    """工具的一次状态变化：started / completed / failed。

    ``run_id`` 是 LangChain 给的调用标识，**只在一次流式请求内有意义**——服务层拿它把
    started 和 completed 对上（审计表上不存它，见 ``repositories/audits.py`` 的说明）。
    """

    run_id: str
    name: str
    status: str
    arguments: dict[str, Any] = field(default_factory=dict)
    output: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AgentTurnCompleted:
    """一轮结束：**本轮新产生的消息**（不含我们传进去的历史）。

    服务层拿它落库——assistant 行、tool 行就是这么来的。
    """

    messages: list[ChatTurnMessage]


AgentEvent = AgentContentDelta | AgentReasoningDelta | AgentToolEvent | AgentTurnCompleted


@runtime_checkable
class ChatAgentRunner(Protocol):
    """编排层眼里的 Agent：给提示词、历史、工具，吐一串事件。

    ``thread_id`` 是给"用 checkpointer 的那套实现"留的口子——它需要它来定位会话状态
    （历史由框架恢复，所以 ``history`` 只传新消息）；**手动管理那套忽略它**（历史自己拼）。
    两套实现共用这一个协议，编排层才能一行不改地切换。
    """

    def stream(
        self,
        *,
        system_prompt: str,
        history: Sequence[ChatTurnMessage],
        tools: Sequence[Any],
        thread_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]: ...


class LangChainChatAgentRunner:
    """``create_agent`` + ``astream_events`` 的真实实现（**手动管理历史**）。"""

    def __init__(self, chat_model: ChatModelProvider) -> None:
        # 拿的是 provider 而不是裸 ChatOpenAI，这样"懒构建 + 配置缺失给可读报错"还在
        self._chat_model = chat_model

    async def stream(
        self,
        *,
        system_prompt: str,
        history: Sequence[ChatTurnMessage],
        tools: Sequence[Any],
        thread_id: str | None = None,  # noqa: ARG002 —— 手动管理用不上：历史由调用方拼好传进来
    ) -> AsyncIterator[AgentEvent]:
        # ⚠️ 参数名是 system_prompt，不是 prompt（1.x 的 create_agent 签名）
        agent = create_agent(
            model=self._chat_model.create_chat_model(),
            tools=list(tools),
            system_prompt=system_prompt,
        )
        payload = {"messages": [to_langchain(item) for item in history]}
        # 传进去几条，就是为了最后能把"本轮新增的"切出来
        input_count = len(payload["messages"])
        final_state: dict[str, Any] | None = None

        async for event in agent.astream_events(payload, version="v2"):
            name = event.get("event")
            data = event.get("data") or {}

            if name == "on_chat_model_stream":
                chunk = data.get("chunk")
                text = message_text(getattr(chunk, "content", ""))
                if text:
                    yield AgentContentDelta(text=text)
                reasoning = extract_reasoning(chunk)
                if reasoning:
                    yield AgentReasoningDelta(text=reasoning)

            elif name == "on_tool_start":
                yield AgentToolEvent(
                    run_id=str(event.get("run_id") or ""),
                    name=str(event.get("name") or ""),
                    status="started",
                    arguments=_as_dict(data.get("input")),
                )

            elif name in ("on_tool_end", "on_tool_error"):
                output = data.get("output")
                yield AgentToolEvent(
                    run_id=str(event.get("run_id") or ""),
                    name=str(event.get("name") or ""),
                    status="completed" if name == "on_tool_end" else "failed",
                    output=message_text(getattr(output, "content", output))[:2000],
                    error=None if name == "on_tool_end" else str(output)[:500],
                    arguments=_as_dict(getattr(output, "additional_kwargs", None)),
                )

            elif name == "on_chain_end" and isinstance(data.get("output"), dict):
                # 顶层图的收尾事件带着完整 state；中途的节点也可能吐 dict，
                # 所以每次都覆盖——最后留下的必然是根节点的那一份
                if "messages" in data["output"]:
                    final_state = data["output"]

        messages = list((final_state or {}).get("messages") or [])
        yield AgentTurnCompleted(messages=[from_langchain(m) for m in messages[input_count:]])


# ---------- 两个方向的转换（LangChain 类型只出现在这下面） ----------


def to_langchain(item: ChatTurnMessage) -> Any:
    if item.role == "system":
        return SystemMessage(content=item.content)
    if item.role == "tool":
        # tool_call_id 为空的话这条消息是坏的（模型端会直接报错），宁可退化成 user 也别炸
        if not item.tool_call_id:
            return HumanMessage(content=item.content)
        return ToolMessage(content=item.content, tool_call_id=item.tool_call_id)
    if item.role == "assistant":
        return AIMessage(content=item.content, tool_calls=list(item.tool_calls or []))
    return HumanMessage(content=item.content)


def from_langchain(message: Any) -> ChatTurnMessage:
    """LangChain 消息 → 我们自己的类型（**落库用**）。"""
    if isinstance(message, ToolMessage):
        return ChatTurnMessage(
            role="tool",
            content=message_text(message.content),
            tool_call_id=getattr(message, "tool_call_id", None),
        )
    if isinstance(message, AIMessage):
        tool_calls = [
            {
                "id": call.get("id"),
                "name": call.get("name"),
                "args": call.get("args") or {},
            }
            for call in (getattr(message, "tool_calls", None) or [])
        ]
        return ChatTurnMessage(
            role="assistant", content=message_text(message.content), tool_calls=tool_calls
        )
    return ChatTurnMessage(role="user", content=message_text(getattr(message, "content", "")))


# ---------- 小工具 ----------


def _as_dict(value: Any) -> dict[str, Any]:
    """工具的入参：不是 dict 就退化成空（宁可审计里少点信息，也不要抛异常打断对话）。"""
    return value if isinstance(value, dict) else {}


def extract_reasoning(chunk: Any) -> str:
    """从 chunk 里抠思维链增量。

    各家的字段名不一样（``reasoning_content`` / ``reasoning`` 都见过），模型没开思考时
    这里一路返回空串，不影响主流程。
    """
    if chunk is None:
        return ""
    for holder in (chunk, getattr(chunk, "additional_kwargs", None)):
        if not holder:
            continue
        value = None
        if isinstance(holder, dict):
            value = holder.get("reasoning_content") or holder.get("reasoning")
        else:
            value = getattr(holder, "reasoning_content", None) or getattr(holder, "reasoning", None)
        if isinstance(value, str) and value:
            return value
    return ""
