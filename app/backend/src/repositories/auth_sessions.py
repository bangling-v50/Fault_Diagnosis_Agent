from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backend.src.models.auth import AuthSessionModel


class AuthSessionRepository:
    """auth_sessions 表：创建、按 token_hash 校验/撤销、按用户批量撤销、刷新 last_seen。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        user_id: int,
        token_hash: str,
        last_seen_at: datetime,
        expires_at: datetime,
    ) -> None:
        row = AuthSessionModel(
            user_id=user_id,
            token_hash=token_hash,
            last_seen_at=last_seen_at,
            expires_at=expires_at,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()

    async def find_valid_by_token_hash(
        self, token_hash: str, now: datetime
    ) -> AuthSessionModel | None:
        stmt = select(AuthSessionModel).where(
            AuthSessionModel.token_hash == token_hash,
            AuthSessionModel.revoked_at.is_(None),
            AuthSessionModel.expires_at > now,
        )
        async with self._session_factory() as session:
            return (await session.scalars(stmt)).one_or_none()

    async def update_last_seen(self, session_id: UUID, now: datetime) -> None:
        stmt = (
            update(AuthSessionModel)
            .where(AuthSessionModel.id == session_id)
            .values(last_seen_at=now)
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def revoke(self, token_hash: str, now: datetime) -> None:
        """登出：把该 token 的会话标记为已撤销。"""
        stmt = (
            update(AuthSessionModel)
            .where(
                AuthSessionModel.token_hash == token_hash,
                AuthSessionModel.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def revoke_all_for_user(self, user_id: int, now: datetime) -> None:
        """撤销某用户的全部有效会话：停用账号或重置密码后调用。"""
        stmt = (
            update(AuthSessionModel)
            .where(
                AuthSessionModel.user_id == user_id,
                AuthSessionModel.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()
