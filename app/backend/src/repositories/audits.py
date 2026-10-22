from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backend.src.models.audits import ToolCallAuditModel


class ToolCallAuditRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        tenant_id: int,
        session_id: int,
        tool_name: str,
        arguments: dict,
    ) -> ToolCallAuditModel:
        """工具开跑：落一条 RUNNING 并记下开始时间。"""
        row = ToolCallAuditModel(
            tenant_id=tenant_id,
            chat_session_id=session_id,
            tool_name=tool_name,
            arguments=arguments,
            status="RUNNING",
            started_at=_utc_now(),
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

    async def finalize(
        self,
        audit_id: int,
        *,
        status: str,
        result_summary: str | None = None,
        result_payload: dict | None = None,
        error_message: str | None = None,
    ) -> ToolCallAuditModel | None:
        """工具跑完：改状态、记结果、**自动算耗时**。查不到返回 None。"""
        async with self._session_factory() as session:
            row = await session.get(ToolCallAuditModel, audit_id)
            if row is None:
                return None
            completed_at = _utc_now()
            row.status = status
            row.result_summary = result_summary
            row.result_payload = result_payload
            row.error_message = error_message
            row.completed_at = completed_at
            row.duration_ms = max(0, int((completed_at - row.started_at).total_seconds() * 1000))
            await session.commit()
            await session.refresh(row)
        return row

    async def attach_message(
        self, audit_id: int, *, chat_message_id: int
    ) -> ToolCallAuditModel | None:
        """把审计挂到它产生的那条 ``role='tool'`` 消息上。"""
        async with self._session_factory() as session:
            row = await session.get(ToolCallAuditModel, audit_id)
            if row is None:
                return None
            row.chat_message_id = chat_message_id
            await session.commit()
            await session.refresh(row)
        return row

    async def list_by_session(
        self, session_id: int, *, limit: int = 100
    ) -> list[ToolCallAuditModel]:
        stmt = (
            select(ToolCallAuditModel)
            .where(ToolCallAuditModel.chat_session_id == session_id)
            .order_by(ToolCallAuditModel.started_at, ToolCallAuditModel.id)
            .limit(limit)
        )
        async with self._session_factory() as session:
            return list((await session.scalars(stmt)).all())


# 统一用带时区的 UTC 时间——库列是 timestamptz，混用 naive datetime 会报错
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
