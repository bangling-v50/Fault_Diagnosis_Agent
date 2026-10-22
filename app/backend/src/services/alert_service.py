from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.backend.src.models import (
    AlertModel,
    BackgroundJobModel,
    DeviceModel,
    DiagnosisTaskModel,
    UserModel,
)
from app.backend.src.repositories.alerts import AlertRepository
from app.backend.src.schemas.alert import DeviceAlertRequest
from app.backend.src.services.diagnosis_service import DiagnosisService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AlertError(Exception):
    """携带机器可读错误码的领域异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class IngestResult:
    """上报处理结果；路由据此组装 ``AlertIngestResponse``。

    ``merged=True`` 表示同一故障的重复上报（只累加次数），此时 ``task`` / ``job`` 为 None。
    """

    alert: AlertModel
    merged: bool
    task: DiagnosisTaskModel | None = None
    job: BackgroundJobModel | None = None


class AlertService:
    def __init__(
        self,
        alert_repo: AlertRepository,
        diagnosis_service: DiagnosisService,
    ) -> None:
        self._alert_repo = alert_repo
        self._diagnosis_service = diagnosis_service

    # ---------- 上报归集 ----------

    async def add_alert(self, device: DeviceModel, payload: DeviceAlertRequest) -> IngestResult:
        """处理一次设备上报：合并到未处理告警，或新建告警并触发诊断。"""
        now = _utcnow()
        occurred = payload.occurred_at or now

        # 同设备同告警码、仍未处理 → 视为同一故障的重复上报，只累加次数，不重复诊断
        existing = await self._alert_repo.find_open_by_code(
            device_id=device.id, alert_code=payload.alert_code
        )
        if existing is not None:
            alert = await self._alert_repo.bump_occurrence(
                alert_id=existing.id, last_seen_at=occurred
            )
            return IngestResult(alert=alert, merged=True)

        # 新故障：落告警（租户从设备反推），再建诊断任务并入队
        alert = await self._alert_repo.create(
            tenant_id=device.tenant_id,
            device_id=device.id,
            alert_code=payload.alert_code,
            alert_level=payload.alert_level,
            alert_name=payload.alert_name,
            source=payload.source,
            raw_message=payload.raw_message,
            sensor_summary=payload.sensor_summary,
            first_seen_at=occurred,
            last_seen_at=occurred,
        )
        task, job = await self._diagnosis_service.create_task_for_alert(
            alert=alert, device=device
        )
        return IngestResult(alert=alert, merged=False, task=task, job=job)

    # ---------- 查询（带租户作用域，规则同设备域） ----------

    async def get_alert(self, actor: UserModel, alert_id: int) -> AlertModel:
        alert = await self._alert_repo.find_by_id(alert_id)
        if alert is None:
            raise AlertError("ALERT_NOT_FOUND", "告警不存在")
        # 查完隔离
        self._ensure_visible(actor, alert)
        return alert

    async def list_alerts(
        self,
        actor: UserModel,
        *,
        page: int,
        page_size: int,
        tenant_id: int | None = None,
        device_id: int | None = None,
        status: str | None = None,
        alert_level: str | None = None,
        keyword: str | None = None,
    ) -> tuple[list[AlertModel], int]:
        scoped_tenant_id = self._scope_tenant(actor, tenant_id)
        return await self._alert_repo.list(
            page=page,
            page_size=page_size,
            tenant_id=scoped_tenant_id,
            device_id=device_id,
            status=status,
            alert_level=alert_level,
            keyword=keyword,
        )

    # ---------- 给聊天 Agent 当工具用 ----------

    async def recent_alerts(
        self, *, tenant_id: int, device_id: int | None = None, limit: int = 5
    ) -> list[AlertModel]:
        """最近若干条告警（**给聊天 Agent 的工具用，不经过 actor 权限层**）。

        作用域由调用方（工具闭包）固定：租户必给，设备可选——不带设备就是"这个租户最近报了什么"。
        这里不收 actor 是刻意的：工具的作用域在建工具时就锁死了，再走一遍 actor 过滤是重复的。
        """
        rows, _ = await self._alert_repo.list(
            page=1, page_size=max(1, limit), tenant_id=tenant_id, device_id=device_id
        )
        return rows

    # ---------- 内部 ----------

    def _scope_tenant(self, actor: UserModel, requested_tenant_id: int | None) -> int | None:
        """operator 只能看本租户；engineer/admin 可跨租户（engineer 按需过滤）。

        与 ``device_service`` 里那份逻辑相同——域一多就该抽到公共处（如
        ``utils/scoping.py``），现在先保持就近可读。
        """
        if actor.role == "operator":
            if actor.tenant_id is None:
                raise AlertError("FORBIDDEN", "账号未绑定租户，无法查看告警")
            if requested_tenant_id is not None and requested_tenant_id != actor.tenant_id:
                raise AlertError("FORBIDDEN", "无权查看其他租户的告警")
            return actor.tenant_id
        return requested_tenant_id

    def _ensure_visible(self, actor: UserModel, alert: AlertModel) -> None:
        if actor.role == "operator" and alert.tenant_id != actor.tenant_id:
            raise AlertError("FORBIDDEN", "无权查看其他租户的告警")
