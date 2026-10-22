from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backend.src.models.alert import AlertModel
from app.backend.src.models.auth import UserModel
from app.backend.src.models.equipment import DeviceModel
from app.backend.src.models.work_orders import WorkOrderModel

# 视为"已结案"的状态——只有这两种才可能带维修结论（根因/方案/结果），也是诊断最想要的证据。
# 与 ``models/work_orders.py`` 的 CHECK 约束保持一致。
CLOSED_STATUSES: tuple[str, ...] = ("RESOLVED", "CLOSED")


class WorkOrderRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ---------- 写入 ----------

    async def create(
        self,
        *,
        tenant_id: int,
        device_id: int,
        title: str,
        alert_id: int | None = None,
        diagnosis_report_id: int | None = None,
        description: str | None = None,
        priority: str = "medium",
        status: str = "OPEN",
        assigned_to: int | None = None,
        created_by: int | None = None,
        confirmed_root_cause: str | None = None,
        solution: str | None = None,
        resolution_result: str | None = None,
        closed_at: datetime | None = None,
    ) -> WorkOrderModel:
        """建一张工单。``tenant_id`` / ``device_id`` / ``title`` 为 NOT NULL。

        ``alert_id`` 务必传：它是工单与"故障码"之间唯一的纽带（见模块 docstring），
        不传就等于这张工单不会进入诊断的历史参考。
        """
        row = WorkOrderModel(
            tenant_id=tenant_id,
            device_id=device_id,
            title=title,
            alert_id=alert_id,
            diagnosis_report_id=diagnosis_report_id,
            description=description,
            priority=priority,
            status=status,
            assigned_to=assigned_to,
            created_by=created_by,
            confirmed_root_cause=confirmed_root_cause,
            solution=solution,
            resolution_result=resolution_result,
            closed_at=closed_at,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
        return row

    # ---------- 查询 ----------

    async def find_by_id(self, work_order_id: int) -> WorkOrderModel | None:
        async with self._session_factory() as session:
            return await session.get(WorkOrderModel, work_order_id)

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        tenant_id: int | None = None,
        device_id: int | None = None,
        status: str | None = None,
        priority: str | None = None,
        assigned_to: int | None = None,
        keyword: str | None = None,
    ) -> tuple[list[tuple[WorkOrderModel, str | None, str | None, str | None]], int]:
        """分页列出工单，**一次 join 带出设备编码/名称与处理人用户名**。

        工单表里只有 ``device_id`` / ``assigned_to`` 两个 id，而列表上要显示"哪台设备、谁在处理"。
        让前端拿着 id 逐条回查就是 N+1；这里一次查完。
        处理人用 **outer join**——``assigned_to`` 可空，没派单的工单不能因此从列表里消失。

        返回 ``([(工单, 设备编码, 设备名, 处理人用户名), …], total)``。
        """
        conditions = []
        if tenant_id is not None:
            conditions.append(WorkOrderModel.tenant_id == tenant_id)
        if device_id is not None:
            conditions.append(WorkOrderModel.device_id == device_id)
        if status:
            conditions.append(WorkOrderModel.status == status)
        if priority:
            conditions.append(WorkOrderModel.priority == priority)
        if assigned_to is not None:
            conditions.append(WorkOrderModel.assigned_to == assigned_to)
        if keyword:
            like = f"%{keyword.strip()}%"
            conditions.append(
                or_(WorkOrderModel.title.ilike(like), WorkOrderModel.description.ilike(like))
            )

        joined = (
            select(
                WorkOrderModel,
                DeviceModel.device_code,
                DeviceModel.device_name,
                UserModel.username,
            )
            .join(DeviceModel, WorkOrderModel.device_id == DeviceModel.id)
            .outerjoin(UserModel, WorkOrderModel.assigned_to == UserModel.id)
            .where(*conditions)
        )
        count_stmt = select(func.count()).select_from(WorkOrderModel).where(*conditions)
        page_stmt = (
            joined.order_by(WorkOrderModel.created_at.desc(), WorkOrderModel.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        async with self._session_factory() as session:
            total = int((await session.scalar(count_stmt)) or 0)
            results = (await session.execute(page_stmt)).all()
        return [
            (order, device_code, device_name, assigned_name)
            for order, device_code, device_name, assigned_name in results
        ], total

    async def update(self, work_order_id: int, fields: dict) -> WorkOrderModel | None:
        """局部更新（改状态、派单、回填结案信息等）；查不到返回 None。"""
        async with self._session_factory() as session:
            row = await session.get(WorkOrderModel, work_order_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await session.commit()
            # onupdate=func.now() 会让 updated_at 失效，须在会话内刷新（同其它仓储）
            await session.refresh(row)
        return row

    async def history_by_code(
        self,
        *,
        device_id: int,
        alert_code: str,
        limit: int = 5,
        tenant_id: int | None = None,
        exclude_alert_id: int | None = None,
    ) -> list[WorkOrderModel]:
        """同设备、同告警码的历史维修工单（诊断工具③的输入）。

        两个排序意图：

        1. **已结案的排前面**——工单的价值在 ``confirmed_root_cause`` / ``solution`` /
           ``resolution_result``，而这三列只有结案后才有值；如果只按时间倒序，一堆空壳的
           新工单会把唯一那条有用的维修记录挤出 top N；
        2. 同组内按 ``created_at`` 倒序（``id`` 兜底），最近发生的先看。

        ``exclude_alert_id`` 用于排掉"本次故障自己产生的那张工单"（重诊断时会遇到）。

        ``tenant_id`` 是可选的第二道保险：``device_id`` 本身已经隐含租户，传进来只是让
        "跨租户查不到"这件事在 SQL 层面也成立。
        """
        code = alert_code.strip()
        if not code:
            return []
        conditions = [
            WorkOrderModel.device_id == device_id,
            AlertModel.alert_code == code,
        ]
        if tenant_id is not None:
            conditions.append(WorkOrderModel.tenant_id == tenant_id)
        if exclude_alert_id is not None:
            conditions.append(WorkOrderModel.alert_id != exclude_alert_id)

        # 结案标记：0/1 排序键，比在 Python 里再排一次更省事，也避免 LIMIT 前先截断
        is_closed = case((WorkOrderModel.status.in_(CLOSED_STATUSES), 1), else_=0)
        stmt = (
            select(WorkOrderModel)
            .join(AlertModel, WorkOrderModel.alert_id == AlertModel.id)
            .where(*conditions)
            .order_by(is_closed.desc(), WorkOrderModel.created_at.desc(), WorkOrderModel.id.desc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            return list((await session.scalars(stmt)).all())
