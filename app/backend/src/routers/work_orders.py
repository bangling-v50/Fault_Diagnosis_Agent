from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status

from app.backend.src.containers import Container
from app.backend.src.models import UserModel
from app.backend.src.models.work_orders import WorkOrderModel
from app.backend.src.schemas.common import APIResponse, PageData, PageQuery, ok, page_data
from app.backend.src.schemas.work_order import (
    WorkOrderCreateRequest,
    WorkOrderPublic,
    WorkOrderUpdateRequest,
)
from app.backend.src.services.work_order_service import WorkOrderError
from app.backend.src.utils.auth import get_container, get_current_user, require_roles
from app.backend.src.utils.errors import to_http

router = APIRouter(prefix="/work_orders", tags=["work_orders"])

# 查询：任何登录用户（可见范围由服务层按租户裁剪）
_AnyUser = Annotated[UserModel, Depends(get_current_user)]
# 写入：仅售后技术人员与管理员
_EngineerOrAdmin = Annotated[UserModel, Depends(require_roles("engineer"))]


def _to_public(
    order: WorkOrderModel,
    *,
    device_code: str | None = None,
    device_name: str | None = None,
    assigned_name: str | None = None,
) -> WorkOrderPublic:
    """ORM 行 → 响应体，顺带填上 join 出来的那几个展示字段。"""
    return WorkOrderPublic.model_validate(order).model_copy(
        update={
            "device_code": device_code,
            "device_name": device_name,
            "assigned_to_name": assigned_name,
        }
    )


@router.get("", response_model=APIResponse[PageData[WorkOrderPublic]])
async def list_work_orders(
    page: Annotated[PageQuery, Depends()],
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
    tenant_id: int | None = None,
    device_id: int | None = None,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(OPEN|ASSIGNED|IN_PROGRESS|RESOLVED|CLOSED)$")
    ] = None,
    priority: Annotated[str | None, Query(pattern="^(low|medium|high|urgent)$")] = None,
    assigned_to: int | None = None,
    keyword: str | None = None,
) -> APIResponse:
    """分页列出工单（operator 只能看到本租户）。

    ``status`` 是 FastAPI 的保留名，所以参数名用 ``status_filter`` + ``alias``（同告警/诊断域）。
    """
    try:
        rows, total = await container.work_order_service.list_orders(
            actor,
            page=page.page,
            page_size=page.page_size,
            tenant_id=tenant_id,
            device_id=device_id,
            status=status_filter,
            priority=priority,
            assigned_to=assigned_to,
            keyword=keyword,
        )
    except WorkOrderError as exc:
        raise to_http(exc) from exc
    items = [
        _to_public(order, device_code=code, device_name=name, assigned_name=assigned)
        for order, code, name, assigned in rows
    ]
    return ok(page_data(items=items, total=total, page=page.page, page_size=page.page_size))


@router.get("/{work_order_id}", response_model=APIResponse[WorkOrderPublic])
async def get_work_order(
    work_order_id: int,
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        order = await container.work_order_service.get_order(actor, work_order_id)
    except WorkOrderError as exc:
        raise to_http(exc) from exc
    return ok(_to_public(order))


@router.post(
    "", response_model=APIResponse[WorkOrderPublic], status_code=http_status.HTTP_201_CREATED
)
async def create_work_order(
    body: WorkOrderCreateRequest,
    actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """建单。设备归属由服务端从 ``device_id`` 反推，请求体不能自述租户。

    诊断详情页的「生成工单」走的就是这个接口——它会把 ``alert_id`` 与 ``diagnosis_report_id``
    一并带上，这张工单才会被后续诊断当成历史经验检索到。
    """
    try:
        order = await container.work_order_service.create_order(
            actor,
            device_id=body.device_id,
            title=body.title,
            description=body.description,
            priority=body.priority,
            alert_id=body.alert_id,
            diagnosis_report_id=body.diagnosis_report_id,
            assigned_to=body.assigned_to,
        )
    except WorkOrderError as exc:
        raise to_http(exc) from exc
    return ok(_to_public(order), message="工单已创建")


@router.put("/{work_order_id}", response_model=APIResponse[WorkOrderPublic])
async def update_work_order(
    work_order_id: int,
    body: WorkOrderUpdateRequest,
    actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """局部更新：派单、改优先级、状态流转、结案。

    **结案（``status=CLOSED``）要求已有「确认根因」与「处理方案」**，否则 400——这两列是
    案例沉淀的输入，不能空着关单。
    """
    try:
        order = await container.work_order_service.update_order(
            actor, work_order_id, body.model_dump(exclude_unset=True)
        )
    except WorkOrderError as exc:
        raise to_http(exc) from exc
    return ok(_to_public(order), message="工单已更新")
