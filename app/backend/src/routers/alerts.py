from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.backend.src.containers import Container
from app.backend.src.models import DeviceModel
from app.backend.src.models.auth import UserModel
from app.backend.src.schemas.alert import AlertIngestResponse, AlertPublic, DeviceAlertRequest
from app.backend.src.schemas.common import APIResponse, PageData, PageQuery, ok, page_data
from app.backend.src.services.alert_service import AlertError
from app.backend.src.utils.auth import get_container, get_current_device, get_current_user
from app.backend.src.utils.errors import to_http

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.post("/devices/alerts")
async def alert(data: DeviceAlertRequest,
                device: DeviceModel = Depends(get_current_device),
                container: Container = Depends(get_container)):
    """工控机上报入口：保存告警信息，并建立故障诊断任务和后台任务。"""
    try:
        alert_result = await container.alert_service.add_alert(device, data)
    except AlertError as exc:
        raise to_http(exc) from exc
    return ok(AlertIngestResponse(alert_id=alert_result.alert.id, merged=alert_result.merged,
                                  occurrence_count=alert_result.alert.occurrence_count,
                                  diagnosis_task_id=alert_result.task.id if alert_result.task else None,
                                  job_id=alert_result.job.id if alert_result.job else None), message="上报成功")

# 用户端，非工控机，没有device的token了
@router.get("/get_alerts_list", response_model=APIResponse[PageData[AlertPublic]])
async def get_alert_list(
    page: Annotated[PageQuery, Depends()],
    user: UserModel = Depends(get_current_user),
    container: Container = Depends(get_container),
    tenant_id: int | None = None,
    device_id: int | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    alert_level: Annotated[str | None, Query(pattern="^(info|warning|critical)$")] = None,
    keyword: str | None = None,
) -> APIResponse:
    """分页查询告警列表。

    - 分页参数用 `PageQuery`（`page` / `page_size`），与 devices/users/tenants 一致；
    - **租户作用域在服务层**：operator 只能看到本租户，engineer/admin 可跨租户；
    - `status` 是 FastAPI 的保留名（状态码模块），所以参数名用 `status_filter` + `alias="status"`。
    """
    try:
        rows, total = await container.alert_service.list_alerts(
            user,
            page=page.page,
            page_size=page.page_size,
            tenant_id=tenant_id,
            device_id=device_id,
            status=status_filter,
            alert_level=alert_level,
            keyword=keyword,
        )
    except AlertError as exc:
        raise to_http(exc) from exc
    return ok(
        page_data(
            items=[AlertPublic.model_validate(row) for row in rows],
            total=total,
            page=page.page,
            page_size=page.page_size,
        )
    )


@router.get("/get_alert/{alert_id}")
async def get_alert(alert_id: int,
                    user: UserModel = Depends(get_current_user),
                    container: Container = Depends(get_container)):
    try:
        result = await container.alert_service.get_alert(user, alert_id)
    except AlertError as exc:
        raise to_http(exc) from exc
    return ok(AlertPublic.model_validate(result))


@router.put("/update_alert/{alert_id}")
async def update_alert(alert_id: int, data, user: UserModel = Depends(get_current_user)):
    pass
