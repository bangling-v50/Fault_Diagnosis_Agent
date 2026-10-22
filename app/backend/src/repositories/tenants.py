from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backend.src.models.auth import TenantModel


class TenantRepository:
    """tenants 表：查找 / 分页列表 / 新建 / 局部更新。

    每个方法独立开一个会话并自己提交（src1 形态）；唯一键冲突（tenant_code）抛
    IntegrityError 交给业务层翻译。``update`` 用于局部更新与改状态。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def find_by_code(self, tenant_code: str) -> TenantModel | None:
        stmt = select(TenantModel).where(TenantModel.tenant_code == tenant_code)
        async with self._session_factory() as session:
            return (await session.scalars(stmt)).one_or_none()

    async def find_by_id(self, tenant_id: int) -> TenantModel | None:
        async with self._session_factory() as session:
            return await session.get(TenantModel, tenant_id)

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
        keyword: str | None = None,
    ) -> tuple[list[TenantModel], int]:
        conditions = []
        if status:
            conditions.append(TenantModel.status == status)
        if keyword:
            like = f"%{keyword.strip()}%"
            conditions.append(
                or_(
                    TenantModel.tenant_code.ilike(like),
                    TenantModel.tenant_name.ilike(like),
                )
            )
        count_stmt = select(func.count()).select_from(TenantModel).where(*conditions)
        page_stmt = (
            select(TenantModel)
            .where(*conditions)
            .order_by(TenantModel.id)
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
        tenant_code: str,
        tenant_name: str,
        region: str | None,
        address: str | None,
        contact_person: str | None,
        contact_phone: str | None,
    ) -> TenantModel:
        row = TenantModel(
            tenant_code=tenant_code,
            tenant_name=tenant_name,
            region=region,
            address=address,
            contact_person=contact_person,
            contact_phone=contact_phone,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
        return row

    async def update(self, tenant_id: int, fields: dict) -> TenantModel | None:
        """局部更新；查不到返回 None。"""
        async with self._session_factory() as session:
            row = await session.get(TenantModel, tenant_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await session.commit()
            # onupdate=func.now() 会让 updated_at 失效，必须在会话内刷新，
            # 否则会话关闭后再取该属性会抛 DetachedInstanceError。
            await session.refresh(row)
        return row

    async def set_status(self, tenant_id: int, status: str) -> TenantModel | None:
        return await self.update(tenant_id, {"status": status})
