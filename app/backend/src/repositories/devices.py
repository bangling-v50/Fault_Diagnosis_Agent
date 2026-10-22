from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backend.src.models.equipment import DeviceModel


class DeviceRepository:
    """devices 表：查找 / 分页列表 / 新建 / 局部更新。唯一键冲突（device_code）抛 IntegrityError。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def find_by_id(self, device_id: int) -> DeviceModel | None:
        async with self._session_factory() as session:
            return await session.get(DeviceModel, device_id)

    async def find_by_code(self, device_code: str) -> DeviceModel | None:
        stmt = select(DeviceModel).where(DeviceModel.device_code == device_code)
        async with self._session_factory() as session:
            return (await session.scalars(stmt)).one_or_none()

    async def find_by_token_hash(self, token_hash: str) -> DeviceModel | None:
        """按上报令牌哈希反查设备（依赖 uq_devices_device_token_hash 唯一索引）。"""
        stmt = select(DeviceModel).where(DeviceModel.device_token_hash == token_hash)
        async with self._session_factory() as session:
            return (await session.scalars(stmt)).one_or_none()

    async def set_token_hash(self, device_id: int, token_hash: str) -> DeviceModel | None:
        """写入/更换设备令牌哈希；换掉即意味着旧令牌立刻失效。"""
        return await self.update(device_id, {"device_token_hash": token_hash})

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        tenant_id: int | None = None,
        status: str | None = None,
        keyword: str | None = None,
    ) -> tuple[list[DeviceModel], int]:
        conditions = []
        if tenant_id is not None:
            conditions.append(DeviceModel.tenant_id == tenant_id)
        if status:
            conditions.append(DeviceModel.status == status)
        if keyword:
            like = f"%{keyword.strip()}%"
            conditions.append(
                or_(
                    DeviceModel.device_code.ilike(like),
                    DeviceModel.device_name.ilike(like),
                    DeviceModel.device_model.ilike(like),
                    DeviceModel.serial_number.ilike(like),
                )
            )
        count_stmt = select(func.count()).select_from(DeviceModel).where(*conditions)
        page_stmt = (
            select(DeviceModel)
            .where(*conditions)
            .order_by(DeviceModel.id)
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
        tenant_id: int,
        device_code: str,
        device_name: str | None,
        device_model: str | None,
        serial_number: str | None,
        install_location: str | None,
        install_date: datetime | None,
        status: str = "offline",
    ) -> DeviceModel:
        row = DeviceModel(
            tenant_id=tenant_id,
            device_code=device_code,
            device_name=device_name,
            device_model=device_model,
            serial_number=serial_number,
            install_location=install_location,
            install_date=install_date,
            status=status,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
        return row

    async def update(self, device_id: int, fields: dict) -> DeviceModel | None:
        """局部更新；查不到返回 None。"""
        async with self._session_factory() as session:
            row = await session.get(DeviceModel, device_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await session.commit()
            # onupdate=func.now() 会让 updated_at 失效，须在会话内刷新（同 tenants/users）。
            await session.refresh(row)
        return row
