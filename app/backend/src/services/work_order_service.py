from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.backend.src.models import UserModel
from app.backend.src.models.work_orders import WorkOrderModel
from app.backend.src.repositories.alerts import AlertRepository
from app.backend.src.repositories.devices import DeviceRepository
from app.backend.src.repositories.users import UserRepository
from app.backend.src.repositories.work_orders import CLOSED_STATUSES, WorkOrderRepository

# 工单"现象描述"在提示词里的截断长度：够模型判断故障场景，又不至于把上下文塞满
MAX_DESCRIPTION_CHARS = 200

# 能接工单的角色：工单是售后侧处理的，操作员不接单
ASSIGNEE_ROLES = ("engineer", "admin")


class WorkOrderError(Exception):
    """携带机器可读错误码的领域异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class WorkOrderHistoryItem:
    """一张历史工单的瘦视图（字段含义见 ``models/work_orders.py``）。"""

    work_order_id: int
    alert_id: int | None
    alert_code: str
    title: str
    status: str
    priority: str
    created_at: str | None  # ISO 字符串（见模块 docstring 第 2 条）
    closed_at: str | None
    description: str | None  # 已截断
    confirmed_root_cause: str | None
    solution: str | None
    resolution_result: str | None

    @property
    def closed(self) -> bool:
        """是否已结案——只有结案的工单才可能有维修结论。"""
        return self.status in CLOSED_STATUSES


class WorkOrderService:
    def __init__(
        self,
        *,
        work_order_repo: WorkOrderRepository,
        device_repo: DeviceRepository,
        user_repo: UserRepository,
        alert_repo: AlertRepository,
    ) -> None:
        self._work_order_repo = work_order_repo
        # 建单要反推租户（从设备）并校验告警归属，所以这两个是硬依赖
        self._device_repo = device_repo
        self._user_repo = user_repo
        self._alert_repo = alert_repo

    # ---------- 建单 / 查询 / 流转（界面用） ----------

    async def create_order(
        self,
        actor: UserModel,
        *,
        device_id: int,
        title: str,
        description: str | None = None,
        priority: str = "medium",
        alert_id: int | None = None,
        diagnosis_report_id: int | None = None,
        assigned_to: int | None = None,
    ) -> WorkOrderModel:
        """建一张工单。**租户从设备反推**，请求体说了不算（见模块 docstring 规则 1）。"""
        device = await self._device_repo.find_by_id(device_id)
        if device is None:
            raise WorkOrderError("DEVICE_NOT_FOUND", f"设备不存在：id={device_id}")

        if alert_id is not None:
            alert = await self._alert_repo.find_by_id(alert_id)
            if alert is None:
                raise WorkOrderError("ALERT_NOT_FOUND", f"告警不存在：id={alert_id}")
            if alert.device_id != device_id:
                # 串了设备的工单会让另一台设备的诊断读到不相干的维修记录
                raise WorkOrderError(
                    "ALERT_DEVICE_MISMATCH", f"告警 {alert_id} 不属于设备 {device_id}"
                )
        if assigned_to is not None:
            await self._ensure_assignable(assigned_to)

        return await self._work_order_repo.create(
            tenant_id=device.tenant_id,
            device_id=device_id,
            title=title,
            description=description,
            priority=priority,
            alert_id=alert_id,
            diagnosis_report_id=diagnosis_report_id,
            assigned_to=assigned_to,
            created_by=actor.id,
        )

    async def get_order(self, actor: UserModel, work_order_id: int) -> WorkOrderModel:
        order = await self._work_order_repo.find_by_id(work_order_id)
        if order is None:
            raise WorkOrderError("WORK_ORDER_NOT_FOUND", f"工单不存在：id={work_order_id}")
        self._ensure_visible(actor, order)
        return order

    async def list_orders(
        self,
        actor: UserModel,
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
        scoped_tenant_id = self._scope_tenant(actor, tenant_id)
        return await self._work_order_repo.list(
            page=page,
            page_size=page_size,
            tenant_id=scoped_tenant_id,
            device_id=device_id,
            status=status,
            priority=priority,
            assigned_to=assigned_to,
            keyword=keyword,
        )

    async def update_order(
        self, actor: UserModel, work_order_id: int, fields: dict
    ) -> WorkOrderModel:
        """局部更新：派单、改优先级、状态流转、回填结案信息。

        **结案（``CLOSED``）有前置条件**：必须已经有确认根因与处理方案。这两列是案例沉淀的
        输入，允许"没填就关单"等于允许沉淀出一份没有内容的案例。
        """
        order = await self._work_order_repo.find_by_id(work_order_id)
        if order is None:
            raise WorkOrderError("WORK_ORDER_NOT_FOUND", f"工单不存在：id={work_order_id}")
        self._ensure_visible(actor, order)

        fields = dict(fields)
        assignee = fields.get("assigned_to")
        if assignee is not None and assignee != order.assigned_to:
            await self._ensure_assignable(assignee)

        status = fields.get("status")
        if status is not None:
            if status == "CLOSED":
                root_cause = fields.get("confirmed_root_cause", order.confirmed_root_cause)
                solution = fields.get("solution", order.solution)
                if not root_cause or not solution:
                    raise WorkOrderError(
                        "WORK_ORDER_NOT_RESOLVED",
                        "结案前必须填写「确认根因」与「处理方案」",
                    )
                fields["closed_at"] = datetime.now(timezone.utc)
            else:
                # 从 CLOSED 改回处理中：关单时间必须清掉，否则"结案时间"会指向一次已撤销的结案
                fields["closed_at"] = None

        updated = await self._work_order_repo.update(work_order_id, fields)
        if updated is None:  # 理论上不会（上面刚查过），并发删除时兜底
            raise WorkOrderError("WORK_ORDER_NOT_FOUND", f"工单不存在：id={work_order_id}")
        return updated

    # ---------- 内部 ----------

    async def _ensure_assignable(self, user_id: int) -> None:
        """接单人必须是启用状态的 engineer/admin——操作员不接单。"""
        user = await self._user_repo.find_by_id(user_id)
        if user is None:
            raise WorkOrderError("USER_NOT_FOUND", f"用户不存在：id={user_id}")
        if user.status != "active":
            raise WorkOrderError("ASSIGNEE_NOT_ALLOWED", "该账号已停用，不能接单")
        if user.role not in ASSIGNEE_ROLES:
            raise WorkOrderError("ASSIGNEE_NOT_ALLOWED", "只有售后技术人员或管理员可以接单")

    def _scope_tenant(self, actor: UserModel, requested_tenant_id: int | None) -> int | None:
        """operator 只能看本租户；engineer/admin 可跨租户（engineer 按需过滤）。

        与 ``alert_service`` / ``device_service`` / ``diagnosis_service`` 里那几份逻辑相同——
        域一多就该抽到公共处（如 ``utils/scoping.py``），现在继续就近可读。
        """
        if actor.role == "operator":
            if actor.tenant_id is None:
                raise WorkOrderError("FORBIDDEN", "账号未绑定租户，无法查看工单")
            if requested_tenant_id is not None and requested_tenant_id != actor.tenant_id:
                raise WorkOrderError("FORBIDDEN", "无权查看其他租户的工单")
            return actor.tenant_id
        return requested_tenant_id

    def _ensure_visible(self, actor: UserModel, order: WorkOrderModel) -> None:
        if actor.role == "operator" and order.tenant_id != actor.tenant_id:
            raise WorkOrderError("FORBIDDEN", "无权查看其他租户的工单")

    # ---------- 给诊断当工具用 ----------

    async def history_by_code(
        self,
        *,
        device_id: int,
        alert_code: str,
        limit: int = 5,
        tenant_id: int | None = None,
        exclude_alert_id: int | None = None,
    ) -> list[WorkOrderHistoryItem]:
        """查该设备历史上同一告警码的维修工单，已结案的排在前面。

        查不到**不报错**，返回空列表——"这类故障以前没修过"本身就是有用的信息
        （工具会把它转成一句明确的话告诉模型，防止模型编造维修记录）。
        """
        rows = await self._work_order_repo.history_by_code(
            device_id=device_id,
            alert_code=alert_code,
            limit=max(1, limit),
            tenant_id=tenant_id,
            exclude_alert_id=exclude_alert_id,
        )
        code = alert_code.strip()
        return [
            WorkOrderHistoryItem(
                work_order_id=row.id,
                alert_id=row.alert_id,
                alert_code=code,
                title=row.title,
                status=row.status,
                priority=row.priority,
                created_at=_iso(row.created_at),
                closed_at=_iso(row.closed_at),
                description=_truncate(row.description),
                confirmed_root_cause=row.confirmed_root_cause,
                solution=row.solution,
                resolution_result=row.resolution_result,
            )
            for row in rows
        ]


# 统一把时间转成 ISO 字符串（None 原样返回）；带时区的值会带 +00:00 后缀，可直接入库
def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# 截断长文本，末尾加省略号表明"还有下文"（别让模型以为这就是全文）
def _truncate(value: str | None, limit: int = MAX_DESCRIPTION_CHARS) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text if len(text) <= limit else f"{text[:limit]}…"
