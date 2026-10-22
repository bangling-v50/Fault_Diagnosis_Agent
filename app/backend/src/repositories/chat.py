from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backend.src.models.chat import (
    ChatMessageModel,
    ChatSessionModel,
    UserMemoryModel,
)


class ChatSessionRepository:
    """``chat_sessions``：会话本体 + 它的压缩状态 + 长期记忆提炼标记。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        tenant_id: int,
        user_id: int,
        thread_id: str,
        title: str | None = None,
        device_id: int | None = None,
        alert_id: int | None = None,
        diagnosis_report_id: int | None = None,
    ) -> ChatSessionModel:
        """建会话。

        ``thread_id`` 是 ``chat:{uuid4}``（NOT NULL + unique）——**目前不上 checkpointer，
        它暂时只是个稳定标识**；将来要接 HITL 时它就直接是 LangGraph 的 thread 键。

        ``device_id`` / ``alert_id`` / ``diagnosis_report_id`` 是"带上下文发起"用的：
        从诊断详情页点「追问」进来时带上，模型一上来就知道在聊哪台设备的哪次诊断。
        """
        row = ChatSessionModel(
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=thread_id,
            title=title,
            device_id=device_id,
            alert_id=alert_id,
            diagnosis_report_id=diagnosis_report_id,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
        return row

    async def find_by_id(self, session_id: int) -> ChatSessionModel | None:
        async with self._session_factory() as session:
            return await session.get(ChatSessionModel, session_id)

    async def list_by_user(self, user_id: int, *, limit: int = 50) -> list[ChatSessionModel]:
        """某用户的会话列表，最近活跃的在前。"""
        stmt = (
            select(ChatSessionModel)
            .where(ChatSessionModel.user_id == user_id)
            .order_by(ChatSessionModel.updated_at.desc(), ChatSessionModel.id.desc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            return list((await session.scalars(stmt)).all())

    async def find_extractable(
        self, user_id: int, *, idle_before: datetime
    ) -> ChatSessionModel | None:
        """找**一个**值得提炼长期记忆的历史会话：最近活跃、已闲置一段时间、且还没提炼过。

        "只提炼一个"是刻意的：如果用户攒了五个短会话，开一次新对话难道提炼五次？
        那既慢又费钱。规则定成"最近的那个还没提炼过的"，``memory_extracted_at`` 保证
        同一个会话不会被反复提炼（否则记忆库里会堆出一堆重复条目）。
        """
        stmt = (
            select(ChatSessionModel)
            .where(
                ChatSessionModel.user_id == user_id,
                ChatSessionModel.memory_extracted_at.is_(None),
                ChatSessionModel.updated_at < idle_before,
            )
            .order_by(ChatSessionModel.updated_at.desc())
            .limit(1)
        )
        async with self._session_factory() as session:
            return (await session.scalars(stmt)).one_or_none()

    async def update_title(self, session_id: int, title: str) -> ChatSessionModel | None:
        return await self._update(session_id, {"title": title})

    async def set_memory_mode(self, session_id: int, mode: str) -> ChatSessionModel | None:
        return await self._update(session_id, {"memory_mode": mode})

    async def update_memory_state(
        self,
        session_id: int,
        *,
        memory_summary: str | None,
        compacted_message_count: int,
        context_tokens: int,
        last_compacted_at: datetime | None,
    ) -> ChatSessionModel | None:
        """压缩完把四样一起写回：摘要、游标、当前 token 占用、压缩时间。

        游标 ``compacted_message_count`` 的语义是"**按 id 升序的前 N 条消息已被摘要覆盖**"。
        注意它数的是**全部消息**（含 tool 行），不是"前端看到的那几条"——两套口径混用会让
        游标错位，模型那边就会重复看到已压缩的内容。
        """
        return await self._update(
            session_id,
            {
                "memory_summary": memory_summary,
                "compacted_message_count": compacted_message_count,
                "context_tokens": context_tokens,
                "last_compacted_at": last_compacted_at,
            },
        )

    async def mark_memory_extracted(self, session_id: int) -> ChatSessionModel | None:
        """标记"这个会话的长期记忆已提炼过"，避免下次又被拎出来提炼一遍。"""
        return await self._update(session_id, {"memory_extracted_at": _utc_now()})

    async def delete(self, session_id: int) -> bool:
        """删会话。

        消息按 FK CASCADE 一起删；**提炼出来的长期记忆不删**——它的 ``source_session_id``
        置空，记忆本身留着（那是用户级资产，不该随着一次会话被抹掉）。
        """
        async with self._session_factory() as session:
            row = await session.get(ChatSessionModel, session_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
        return True

    async def _update(self, session_id: int, fields: dict) -> ChatSessionModel | None:
        async with self._session_factory() as session:
            row = await session.get(ChatSessionModel, session_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await session.commit()
            # onupdate=func.now() 会让 updated_at 失效，须在会话内刷新（同其它仓储）
            await session.refresh(row)
        return row


class ChatMessageRepository:
    """``chat_messages``：一条消息一行，含 ``role='tool'`` 的工具结果。

    一轮带工具调用的对话在表里是这样的（四行）：

        user → assistant（tool_calls 存在 metadata 里）→ tool（结果）→ assistant（最终回答）

    工具行进表是**为了回放**：用户追问"你刚才查到的那个工单里方案是什么"时，
    模型必须看得见上一轮的工具结果，否则它只能重新查一遍或者干脆瞎编。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def append(
        self,
        *,
        session_id: int,
        role: str,
        content: str,
        metadata: dict | None = None,
    ) -> ChatMessageModel:
        row = ChatMessageModel(
            session_id=session_id,
            role=role,
            content=content,
            metadata_json=metadata or {},
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(row)
        return row

    async def list_by_session(
        self, session_id: int, *, include_tool: bool = False
    ) -> list[ChatMessageModel]:
        """会话的全部消息（按落盘顺序）。

        ``include_tool=False``（默认）把工具行过滤掉——**给前端看的历史是"谁说了什么"，
        不是"模型当时看到了什么"**。要回放/调试时传 ``True``。
        """
        stmt = select(ChatMessageModel).where(ChatMessageModel.session_id == session_id)
        if not include_tool:
            stmt = stmt.where(ChatMessageModel.role != "tool")
        stmt = stmt.order_by(ChatMessageModel.id)
        async with self._session_factory() as session:
            return list((await session.scalars(stmt)).all())

    async def list_for_model(
        self, session_id: int, *, offset: int = 0, limit: int | None = None
    ) -> list[ChatMessageModel]:
        """**喂给模型**的那一份：从压缩游标之后开始，含 tool 行。

        ``offset`` 传会话的 ``compacted_message_count``——被摘要覆盖的旧消息不再进上下文，
        但它们**仍然在库里**（前端照样能翻到）。
        """
        stmt = (
            select(ChatMessageModel)
            .where(ChatMessageModel.session_id == session_id)
            .order_by(ChatMessageModel.id)
            .offset(offset)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._session_factory() as session:
            return list((await session.scalars(stmt)).all())

    async def count(self, session_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(ChatMessageModel)
            .where(ChatMessageModel.session_id == session_id)
        )
        async with self._session_factory() as session:
            return int((await session.scalar(stmt)) or 0)

    async def clear(self, session_id: int) -> int:
        """清空某个会话的消息，返回删除条数（压缩状态由调用方一并重置）。"""
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(ChatMessageModel).where(ChatMessageModel.session_id == session_id)
                    )
                ).all()
            )
            for row in rows:
                await session.delete(row)
            await session.commit()
        return len(rows)


class UserMemoryRepository:
    """``user_memories``：跨会话的长期记忆。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        tenant_id: int,
        user_id: int,
        kind: str,
        content: str,
        source_session_id: int | None = None,
    ) -> UserMemoryModel:
        row = UserMemoryModel(
            tenant_id=tenant_id,
            user_id=user_id,
            kind=kind,
            content=content,
            source_session_id=source_session_id,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(row)
        return row

    async def list_active(self, user_id: int, *, limit: int = 50) -> list[UserMemoryModel]:
        """当前有效的记忆（注入提示词、以及给用户看的都是这一份）。"""
        stmt = (
            select(UserMemoryModel)
            .where(UserMemoryModel.user_id == user_id, UserMemoryModel.status == "active")
            .order_by(UserMemoryModel.updated_at.desc(), UserMemoryModel.id.desc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            return list((await session.scalars(stmt)).all())

    async def list_all(self, user_id: int, *, limit: int = 100) -> list[UserMemoryModel]:
        """全部记忆（含已作废的）——给用户的记忆管理界面用。"""
        stmt = (
            select(UserMemoryModel)
            .where(UserMemoryModel.user_id == user_id)
            .order_by(UserMemoryModel.status, UserMemoryModel.updated_at.desc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            return list((await session.scalars(stmt)).all())

    async def find_by_content(self, user_id: int, content: str) -> UserMemoryModel | None:
        """按内容精确匹配（写入前的去重）。

        只是**一道廉价的兜底**——真正防重复靠的是提炼时把"已有记忆"一起喂给模型，
        让它只输出新增的。两道一起上，是因为模型偶尔还是会换个说法重复同一条。
        """
        stmt = (
            select(UserMemoryModel)
            .where(UserMemoryModel.user_id == user_id, UserMemoryModel.content == content)
            .limit(1)
        )
        async with self._session_factory() as session:
            return (await session.scalars(stmt)).one_or_none()

    async def find_by_id(self, memory_id: int) -> UserMemoryModel | None:
        async with self._session_factory() as session:
            return await session.get(UserMemoryModel, memory_id)

    async def archive(self, memory_id: int) -> UserMemoryModel | None:
        """作废一条记忆（模型发现它过时了，或用户自己不要了）。"""
        async with self._session_factory() as session:
            row = await session.get(UserMemoryModel, memory_id)
            if row is None:
                return None
            row.status = "archived"
            await session.commit()
            await session.refresh(row)
        return row

    async def archive_by_contents(
        self, user_id: int, contents: Sequence[str]
    ) -> int:
        """按内容批量作废（模型在提炼结果里标出"这几条过时了"）。返回作废条数。"""
        cleaned = [text.strip() for text in contents if text and text.strip()]
        if not cleaned:
            return 0
        async with self._session_factory() as session:
            result = await session.execute(
                update(UserMemoryModel)
                .where(
                    UserMemoryModel.user_id == user_id,
                    UserMemoryModel.content.in_(cleaned),
                    UserMemoryModel.status == "active",
                )
                .values(status="archived")
                .execution_options(synchronize_session=False)
            )
            await session.commit()
        return int(result.rowcount or 0)

    async def delete(self, memory_id: int) -> bool:
        """物理删除——**用户必须能真的删掉自己的记忆**，所以这里不是软删。"""
        async with self._session_factory() as session:
            row = await session.get(UserMemoryModel, memory_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
        return True


# 统一用带时区的 UTC 时间——库列是 timestamptz，混用 naive datetime 会报错
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
