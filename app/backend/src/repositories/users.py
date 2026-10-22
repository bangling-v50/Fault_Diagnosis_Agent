from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backend.src.models.auth import UserModel


class UserRepository:
    """users 表：查找 / 分页列表 / 创建 / 局部更新。

    唯一键冲突（用户名/邮箱）时抛 IntegrityError 交给业务层翻译。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def find_by_username(self, username: str) -> UserModel | None:
        stmt = select(UserModel).where(UserModel.username == username)
        async with self._session_factory() as session:
            return (await session.scalars(stmt)).one_or_none()

    async def find_by_id(self, user_id: int) -> UserModel | None:
        async with self._session_factory() as session:
            return await session.get(UserModel, user_id)

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        tenant_id: int | None = None,
        role: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
    ) -> tuple[list[UserModel], int]:
        conditions = []
        if tenant_id is not None:
            conditions.append(UserModel.tenant_id == tenant_id)
        if role:
            conditions.append(UserModel.role == role)
        if status:
            conditions.append(UserModel.status == status)
        if keyword:
            like = f"%{keyword.strip()}%"
            conditions.append(
                or_(
                    UserModel.username.ilike(like),
                    UserModel.real_name.ilike(like),
                    UserModel.email.ilike(like),
                )
            )
        count_stmt = select(func.count()).select_from(UserModel).where(*conditions)
        page_stmt = (
            select(UserModel)
            .where(*conditions)
            .order_by(UserModel.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        async with self._session_factory() as session:
            total = int((await session.scalar(count_stmt)) or 0)
            rows = list((await session.scalars(page_stmt)).all())
        return rows, total

    async def create(
        self,
        *,
        tenant_id: int | None,
        username: str,
        password_hash: str,
        real_name: str | None,
        email: str | None,
        role: str = "operator",
    ) -> UserModel:
        row = UserModel(
            tenant_id=tenant_id,
            username=username,
            password_hash=password_hash,
            real_name=real_name,
            email=email,
            role=role,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
        return row

    async def update(self, user_id: int, fields: dict) -> UserModel | None:
        """局部更新；查不到返回 None。"""
        async with self._session_factory() as session:
            row = await session.get(UserModel, user_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await session.commit()
            # 同 tenants.update：刷新 onupdate 列，避免会话关闭后取属性报错。
            await session.refresh(row)
        return row

    async def set_password_hash(self, user_id: int, password_hash: str) -> UserModel | None:
        return await self.update(user_id, {"password_hash": password_hash})
