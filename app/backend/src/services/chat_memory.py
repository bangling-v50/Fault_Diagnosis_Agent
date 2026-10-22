from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from langchain_core.messages.utils import count_tokens_approximately

from app.backend.src.llm_provider.chat import ChatError, ChatModelProvider
from app.backend.src.models.chat import ChatMessageModel, ChatSessionModel
from app.backend.src.repositories.chat import (
    ChatMessageRepository,
    ChatSessionRepository,
    UserMemoryRepository,
)
from app.backend.src.settings import Settings

logger = logging.getLogger(__name__)

# 摘要长度上限（汉字）；再长就不叫摘要了，而且会挤占本就紧张的上下文
MAX_SUMMARY_CHARS = 1200
# 单条长期记忆的长度上限
MAX_MEMORY_CONTENT_CHARS = 200
# 一次提炼最多接受几条新记忆（模型很话痨时会一口气列十条）
MAX_MEMORIES_PER_EXTRACT = 5
# 注入提示词的"已有记忆"条数上限
MAX_EXISTING_MEMORIES = 20
# 用近似算法估 token；中文一个字大约 1 token 上下，这里只要个量级
VALID_MEMORY_KINDS = ("preference", "fact", "constraint")

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class ChatContextLimitReached(Exception):
    """上下文已经塞不下新消息了。

    在**写库之前**抛出——所以超限时用户那条消息不会落库，历史保持自洽，
    不会出现"库里有一句没人回答的话"。
    """


@dataclass(frozen=True, slots=True)
class MemoryState:
    """给前端看的压缩状态（`/chat/sessions/{id}` 里带出去）。"""

    memory_mode: str
    context_tokens: int
    context_window_tokens: int
    usage_percent: float
    compacted_message_count: int
    last_compacted_at: str | None


@dataclass(frozen=True, slots=True)
class MemoryExtraction:
    """一次提炼的结果。"""

    written: int
    archived: int


class ChatMemoryService:
    def __init__(
        self,
        *,
        session_repo: ChatSessionRepository,
        message_repo: ChatMessageRepository,
        memory_repo: UserMemoryRepository,
        chat_model: ChatModelProvider,
        settings: Settings,
    ) -> None:
        self._session_repo = session_repo
        self._message_repo = message_repo
        self._memory_repo = memory_repo
        self._chat_model = chat_model
        self._settings = settings

    # ---------- 发消息前的预算判定与压缩 ----------

    async def prepare(self, session: ChatSessionModel, *, system_prompt: str) -> ChatSessionModel:
        """在**落库用户消息之前**调用：估算用量，必要时压缩，超硬限则抛异常。

        为什么必须在写库前：压缩要拿"未压缩的那一段对话"去做摘要，如果用户消息已经写进去了，
        它会被算进待压缩区间——一旦压缩失败或超限，库里就多了一句没有下文的话。
        """
        history = await self._uncompressed(session)
        # ⚠️ `_estimate` 返回的是 **token 数**，硬限是**百分比**——必须换算后再比，
        # 直接 `usage >= hard_limit` 会拿 800 去比 95，把每一次对话都判成超限
        percent = self._usage_percent(system_prompt, session.memory_summary, history)

        if percent >= self._settings.chat_hard_context_percent:
            # 先试着压一把再判（压缩通常会腾出大量空间）
            if await self._maybe_compact(session, system_prompt, history, force=True):
                session = await self._session_repo.find_by_id(session.id) or session
                history = await self._uncompressed(session)
                percent = self._usage_percent(system_prompt, session.memory_summary, history)
            if percent >= self._settings.chat_hard_context_percent:
                raise ChatContextLimitReached(
                    f"上下文已用 {percent:.0f}%，请新建会话或手动压缩后再继续"
                )

        if await self._maybe_compact(session, system_prompt, history, force=False):
            session = await self._session_repo.find_by_id(session.id) or session
        return session

    async def refresh_usage(
        self, session: ChatSessionModel, *, system_prompt: str
    ) -> ChatSessionModel:
        """一轮回答结束后重算用量（前端那个进度条靠它）。"""
        history = await self._uncompressed(session)
        tokens = int(self._estimate(system_prompt, session.memory_summary, history))
        updated = await self._session_repo.update_memory_state(
            session.id,
            memory_summary=session.memory_summary,
            compacted_message_count=session.compacted_message_count,
            context_tokens=tokens,
            last_compacted_at=session.last_compacted_at,
        )
        return updated or session

    async def set_mode(self, session: ChatSessionModel, mode: str) -> ChatSessionModel:
        updated = await self._session_repo.set_memory_mode(session.id, mode)
        return updated or session

    def prompt_with_memory(self, base_prompt: str, summary: str | None) -> str:
        """把会话摘要拼进 system prompt。

        **和长期记忆分开拼**：摘要属于"这段对话之前发生了什么"（会话级、会不断被重写），
        长期记忆属于"这位用户是谁"（用户级、跨会话）。两者混在一起写会让模型分不清
        "这是这次聊的"还是"这是我一贯知道的"。
        """
        if not summary:
            return base_prompt
        return f"{base_prompt}\n\n## 此前对话的压缩记忆\n{summary}"

    def usage_state(self, session: ChatSessionModel) -> MemoryState:
        window = max(1, self._settings.llm.context_window_tokens)
        return MemoryState(
            memory_mode=session.memory_mode,
            context_tokens=session.context_tokens,
            context_window_tokens=window,
            usage_percent=round(session.context_tokens / window * 100, 1),
            compacted_message_count=session.compacted_message_count,
            last_compacted_at=_iso(session.last_compacted_at),
        )

    # ---------- 长期记忆提炼（三条路径共用这一个） ----------

    async def extract_memories(
        self, session: ChatSessionModel, *, only_uncompacted: bool = False
    ) -> MemoryExtraction:
        """从一个会话的对话里提炼长期记忆。

        三条调用路径：压缩时顺带（``only_uncompacted=True``，只看新压进去的那段）、
        用户显式要求 / 新会话第一句提炼上一个（``only_uncompacted=False``，看整段会话）。
        """
        if only_uncompacted:
            messages = await self._uncompressed(session)
        else:
            messages = await self._message_repo.list_for_model(session.id)

        transcript = format_transcript(messages)
        if not transcript.strip():
            return MemoryExtraction(written=0, archived=0)

        existing = await self._memory_repo.list_active(
            session.user_id, limit=MAX_EXISTING_MEMORIES
        )
        prompt = build_extract_prompt(transcript, existing)
        try:
            text = await self._chat_model.ainvoke(prompt)
        except ChatError as exc:
            # 提炼是"锦上添花"：模型调不通就跳过，绝不让它影响对话本身
            logger.warning("长期记忆提炼失败：%s", exc)
            return MemoryExtraction(written=0, archived=0)

        memories, archived = parse_memory_payload(text)
        result = await self._apply_memories(session, memories, archived)
        await self._session_repo.mark_memory_extracted(session.id)
        return result

    # ---------- 内部 ----------

    async def _uncompressed(self, session: ChatSessionModel) -> list[ChatMessageModel]:
        """压缩游标之后的那一段（含 tool 行——它们也要算进上下文）。"""
        return await self._message_repo.list_for_model(
            session.id, offset=session.compacted_message_count
        )

    async def _maybe_compact(
        self,
        session: ChatSessionModel,
        system_prompt: str,
        history: list[ChatMessageModel],
        *,
        force: bool,
    ) -> bool:
        """按模式判断要不要压缩，要就压。返回是否真的压了。"""
        if not history:
            return False
        settings = self._settings
        if not force:
            completed_turns = sum(1 for m in history if m.role == "assistant")
            percent = self._usage_percent(system_prompt, session.memory_summary, history)
            should = (
                session.memory_mode == "every_30_turns"
                and completed_turns >= settings.chat_compact_after_turns
            ) or (
                session.memory_mode == "context_70_percent"
                and percent >= settings.chat_compact_percent
            )
            if not should:
                return False

        await self._compact(session, system_prompt, history)
        return True

    async def _compact(
        self,
        session: ChatSessionModel,
        system_prompt: str,
        history: list[ChatMessageModel],
    ) -> None:
        """把未压缩的那一段做成增量摘要，并把游标往前推。**不删任何消息。**"""
        transcript = format_transcript(history)
        existing = await self._memory_repo.list_active(
            session.user_id, limit=MAX_EXISTING_MEMORIES
        )
        prompt = _compact_prompt(transcript, session.memory_summary, existing)
        try:
            text = await self._chat_model.ainvoke(prompt)
        except ChatError as exc:
            # 压缩失败不致命：这一轮照常发，只是上下文没变小（下次还会再试）
            logger.warning("聊天上下文压缩失败，本轮跳过：%s", exc)
            return

        summary, memories, archived = parse_compact_payload(text)
        if not summary:
            # 带上原文片段：不带的话没法判断是"模型吐了散文"还是"输出被截断"
            logger.warning(
                "压缩结果里没有摘要，本轮跳过压缩（下一轮还会再试）。原文前 200 字：%s", text[:200]
            )
            return

        # 顺带把长期记忆也写了——**这是零成本的**：本来就要调这一次模型
        await self._apply_memories(session, memories, archived)

        new_cursor = session.compacted_message_count + len(history)
        remaining = await self._message_repo.list_for_model(session.id, offset=new_cursor)
        new_summary = summary[:MAX_SUMMARY_CHARS]
        await self._session_repo.update_memory_state(
            session.id,
            memory_summary=new_summary,
            compacted_message_count=new_cursor,
            context_tokens=int(self._estimate(system_prompt, new_summary, remaining)),
            last_compacted_at=_utc_now(),
        )
        # 压缩这一轮已经把记忆提炼过了，标记掉，免得"新会话第一句"再提炼一遍
        await self._session_repo.mark_memory_extracted(session.id)

    async def _apply_memories(
        self, session: ChatSessionModel, memories: list[dict], archived: list[str]
    ) -> MemoryExtraction:
        """写新记忆、作废旧记忆。去重是**两道**：模型那侧带已有记忆、这里再按内容挡一次。"""
        archived_count = await self._memory_repo.archive_by_contents(session.user_id, archived)
        written = 0
        for item in memories:
            content = str(item.get("content") or "").strip()[:MAX_MEMORY_CONTENT_CHARS]
            if not content:
                continue
            kind = str(item.get("kind") or "").strip().lower()
            if kind not in VALID_MEMORY_KINDS:
                kind = "fact"
            if await self._memory_repo.find_by_content(session.user_id, content) is not None:
                continue
            await self._memory_repo.create(
                tenant_id=session.tenant_id,
                user_id=session.user_id,
                kind=kind,
                content=content,
                source_session_id=session.id,
            )
            written += 1
        return MemoryExtraction(written=written, archived=archived_count)

    def _usage_percent(
        self, system_prompt: str, summary: str | None, history: list[ChatMessageModel]
    ) -> float:
        """当前上下文占用**百分比**。

        所有"要不要压缩 / 还能不能塞"的判断都走它——因为 ``_estimate`` 返回的是 **token 数**，
        而配置里的两个阈值（70 / 95）是**百分比**。这两者直接比会得到一个永远为真的判断
        （800 >= 95），本轮就是这么踩的：每一轮都被判成"上下文超限"。
        """
        tokens = self._estimate(system_prompt, summary, history)
        return tokens / max(1, self._settings.llm.context_window_tokens) * 100

    def _estimate(
        self, system_prompt: str, summary: str | None, history: list[ChatMessageModel]
    ) -> float:
        """估算"这一轮要发给模型的上下文"有多少 token。

        组成：system prompt + （可能有的）记忆摘要 + 未压缩的历史。
        ``count_tokens_approximately`` 是 LangChain 给的粗略估算（不依赖具体分词器），
        够用来判断"到没到阈值"。
        """
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        if summary:
            messages.append({"role": "system", "content": summary})
        messages.extend({"role": item.role, "content": item.content} for item in history)
        try:
            return float(count_tokens_approximately(messages))
        except Exception:  # noqa: BLE001 —— 估算失败不该让对话发不出去
            # 退化成"按字符数粗估"（中文约 1 字 1 token，英文更省）
            return float(sum(len(str(item.get("content") or "")) for item in messages))


# ---------- prompt 与解析（纯函数，好单测） ----------


def format_transcript(messages: list[ChatMessageModel]) -> str:
    """把消息拍成一段纯文本喂给模型。

    **tool 行要带上**（标成"工具返回"）——长期记忆里最有价值的东西常常藏在工具结果里，
    比如"这台设备主轴驱动是 XX 型号"就是查出来的而不是用户说的。
    """
    lines = []
    for message in messages:
        role = {"user": "用户", "assistant": "助手", "tool": "工具返回", "system": "系统"}.get(
            message.role, message.role
        )
        content = (message.content or "").strip()
        if content:
            lines.append(f"{role}：{content}")
    return "\n".join(lines)


def _existing_block(existing: list) -> str:
    if not existing:
        return "（暂无）"
    return "\n".join(f"- {item.content}" for item in existing)


def _compact_prompt(transcript: str, previous_summary: str | None, existing: list) -> str:
    return (
        "你在维护一段工业设备售后对话的长期上下文。请做两件事，只输出一个 JSON 对象。\n\n"
        "**第一件：压缩对话**。把下面的对话压缩成一段中文摘要（不超过 1200 字），"
        "保留用户目标、明确事实、决策、未完成事项、工具查到的关键结果；删掉寒暄与重复。"
        "这是**增量压缩**：已有摘要代表更早的对话，你要把它和新增对话合成一份新的完整摘要。\n\n"
        "**第二件：提炼长期记忆**。挑出其中**值得跨会话长期记住的用户信息**——"
        "只有稳定的才算（身份与职责、长期偏好、常用设备、反复出现的关注点）；"
        "一次性的内容（这一次问的故障、这一次的结论）**不算**。\n"
        "如果某条已有记忆已经过时了（比如用户换了岗位），把它原文列进 `archived`。\n\n"
        f"## 已有摘要\n{previous_summary or '（无）'}\n\n"
        f"## 已有长期记忆\n{_existing_block(existing)}\n\n"
        f"## 新增对话\n{transcript}\n\n"
        "## 输出格式（只输出 JSON，不要解释、不要代码围栏）\n"
        '{"summary": "压缩后的摘要", '
        '"memories": [{"kind": "preference|fact|constraint", "content": "一句话"}], '
        '"archived": ["要作废的已有记忆原文"]}'
    )


def build_extract_prompt(transcript: str, existing: list) -> str:
    return (
        "请从下面这段工业设备售后对话里，提炼**值得跨会话长期记住的用户信息**。\n\n"
        "只有**稳定的**才算：身份与职责（如「负责一号车间」）、长期偏好（如「喜欢先给结论」）、"
        "常用设备与机型、反复出现的关注点。一次性的内容（这一次问的故障、这一次的结论）不算。\n"
        "如果某条已有记忆已经过时，把它原文列进 `archived`。最多提炼 5 条。\n\n"
        f"## 已有长期记忆\n{_existing_block(existing)}\n\n"
        f"## 对话\n{transcript}\n\n"
        "## 输出格式（只输出 JSON，不要解释、不要代码围栏）\n"
        '{"memories": [{"kind": "preference|fact|constraint", "content": "一句话"}], '
        '"archived": ["要作废的已有记忆原文"]}'
    )


def parse_compact_payload(text: str) -> tuple[str | None, list[dict], list[str]]:
    """解析压缩结果。

    **除了解 JSON，还有一条针对"输出被截断"的兜底**：压缩的 prompt 要求同时返回摘要 + 记忆，
    响应体可能很长，被 `max_tokens` 砍掉尾部的概率不低——砍掉之后 JSON 根本不完整，
    `json.loads` 和贪婪正则都救不回来（实测遇到过：整轮压缩被跳过，游标不动）。
    这时用正则**单独把 `summary` 字段抠出来**：摘要才是压缩的真正目的，记忆那部分丢了就丢了。
    """
    payload = _load_json(text)
    summary = str(payload.get("summary") or "").strip() or None
    if summary is None:
        summary = _salvage_summary(text)
    return (
        summary,
        _as_memory_list(payload.get("memories")),
        _as_str_list(payload.get("archived")),
    )


# 从可能的截断 JSON 里单独抠 summary 字段（值里可能含转义引号，所以不能简单用 [^"]*）
_SUMMARY_FIELD = re.compile(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"', re.DOTALL)


def _salvage_summary(text: str) -> str | None:
    matched = _SUMMARY_FIELD.search(text or "")
    if not matched:
        return None
    try:
        # 把捕获到的内容当 JSON 字符串解一次，转义（\" \n 等）才会还原
        decoded = json.loads(f'"{matched.group(1)}"')
    except (json.JSONDecodeError, ValueError):
        decoded = matched.group(1)
    return str(decoded).strip() or None


def parse_memory_payload(text: str) -> tuple[list[dict], list[str]]:
    payload = _load_json(text)
    return _as_memory_list(payload.get("memories")), _as_str_list(payload.get("archived"))


def _load_json(text: str) -> dict:
    """从模型输出里抠 JSON。

    这里刻意写得**宽容**（正抠最外层大括号、失败就返回空字典）：压缩与提炼都是"有则更好"的
    附属动作，解析失败应该降级成"这轮不压缩/不记记忆"，而不是把整轮对话打挂。
    """
    if not text:
        return {}
    candidates = [text.strip()]
    matched = _JSON_BLOCK.search(text)
    if matched:
        candidates.append(matched.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _as_memory_list(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    items: list[dict] = []
    for entry in value[:MAX_MEMORIES_PER_EXTRACT]:
        if isinstance(entry, str):
            items.append({"kind": "fact", "content": entry})
        elif isinstance(entry, dict) and entry.get("content"):
            items.append(
                {"kind": str(entry.get("kind") or "fact"), "content": str(entry["content"])}
            )
    return items


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item).strip()
        for item in value
        if isinstance(item, (str, int)) and str(item).strip()
    ]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
