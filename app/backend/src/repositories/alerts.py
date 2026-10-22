from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backend.src.models.alert import AlertModel

# 「未处理」状态集合。``alerts.status`` 没有 DB 级 CHECK（见 models/alert.py），
# 状态机靠这里约定；以后引入 PROCESSING 等中间态时加进来即可。
DEFAULT_OPEN_STATUSES: tuple[str, ...] = ("NEW",)


class AlertRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ---------- 写入 ----------

    async def create(
        self,
        *,
        tenant_id: int,
        device_id: int,
        alert_code: str,
        alert_level: str,
        alert_name: str | None = None,
        source: str | None = None,
        raw_message: str | None = None,
        sensor_summary: dict | None = None,
        first_seen_at: datetime | None = None,
        last_seen_at: datetime | None = None,
        occurrence_count: int = 1,
        status: str = "NEW",
    ) -> AlertModel:
        """落一条新告警。``alert_level`` 必填（CHECK: info/warning/critical）。

        时间戳请传**带时区**的 datetime（库列是 timestamptz）。
        """
        row = AlertModel(
            tenant_id=tenant_id,
            device_id=device_id,
            alert_code=alert_code,
            alert_level=alert_level,
            alert_name=alert_name,
            source=source,
            raw_message=raw_message,
            sensor_summary=sensor_summary,
            first_seen_at=first_seen_at,
            last_seen_at=last_seen_at,
            occurrence_count=occurrence_count,
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

    async def update(self, alert_id: int, fields: dict) -> AlertModel | None:
        """局部更新（改状态、回填诊断结论等）；查不到返回 None。"""
        async with self._session_factory() as session:
            row = await session.get(AlertModel, alert_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await session.commit()
            # onupdate=func.now() 会让 updated_at 失效，须在会话内刷新（同其它仓储）。
            await session.refresh(row)
        return row

    async def bump_occurrence(self, alert_id: int, *, last_seen_at: datetime) -> AlertModel | None:
        """重复上报合并：``occurrence_count`` +1 并刷新 ``last_seen_at``。

        这里用「读-改-写」而非 SQL 级自增，与仓库其它 update 风格保持一致；
        同一台设备的重复上报是串行的，不存在并发丢失更新。
        """
        async with self._session_factory() as session:
            row = await session.get(AlertModel, alert_id)
            if row is None:
                return None
            row.occurrence_count = row.occurrence_count + 1
            row.last_seen_at = last_seen_at
            await session.commit()
            await session.refresh(row)
        return row

    # ---------- 查询 ----------

    async def find_by_id(self, alert_id: int) -> AlertModel | None:
        async with self._session_factory() as session:
            return await session.get(AlertModel, alert_id)

    async def find_open_by_code(
        self,
        *,
        device_id: int,
        alert_code: str,
        open_statuses: Sequence[str] = DEFAULT_OPEN_STATUSES,
    ) -> AlertModel | None:
        """同设备同告警码、且**仍未处理**的那条告警（合并重复上报用）。

        ⚠️ 状态条件不可省：去掉它就会命中已诊断完的历史告警，把新故障累加到旧记录上，
        诊断任务也不会新建，故障永远不会被诊断。
        """
        stmt = (
            select(AlertModel)
            .where(
                AlertModel.device_id == device_id,
                AlertModel.alert_code == alert_code,
                AlertModel.status.in_(tuple(open_statuses)),
            )
            .order_by(AlertModel.id.desc())
            .limit(1)
        )
        async with self._session_factory() as session:
            return (await session.scalars(stmt)).one_or_none()

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        tenant_id: int | None = None,
        device_id: int | None = None,
        status: str | None = None,
        alert_level: str | None = None,
        keyword: str | None = None,
    ) -> tuple[list[AlertModel], int]:
        conditions = []
        if tenant_id is not None:
            conditions.append(AlertModel.tenant_id == tenant_id)
        if device_id is not None:
            conditions.append(AlertModel.device_id == device_id)
        if status:
            conditions.append(AlertModel.status == status)
        if alert_level:
            conditions.append(AlertModel.alert_level == alert_level)
        if keyword:
            like = f"%{keyword.strip()}%"
            conditions.append(
                or_(
                    AlertModel.alert_code.ilike(like),
                    AlertModel.alert_name.ilike(like),
                )
            )
        count_stmt = select(func.count()).select_from(AlertModel).where(*conditions)
        page_stmt = (
            select(AlertModel)
            .where(*conditions)
            .order_by(AlertModel.created_at.desc(), AlertModel.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        async with self._session_factory() as session:
            total = int((await session.scalar(count_stmt)) or 0)
            rows = list((await session.scalars(page_stmt)).all())
        return rows, total

    async def history_by_code(
        self, *, device_id: int, alert_code: str, limit: int = 10
    ) -> list[AlertModel]:
        """同设备同告警码的最近 N 条告警（诊断工具②的输入）。

        按 ``last_seen_at`` 倒序、``NULLS LAST``（该列可为空）；走索引
        ``ix_alerts_tenant_device_code_last_seen``。
        """
        stmt = (
            select(AlertModel)
            .where(
                AlertModel.device_id == device_id,
                AlertModel.alert_code == alert_code,
            )
            .order_by(AlertModel.last_seen_at.desc().nulls_last(), AlertModel.id.desc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            return list((await session.scalars(stmt)).all())
