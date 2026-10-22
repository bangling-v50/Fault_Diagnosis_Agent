from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.backend.src.containers import Container
from app.backend.src.models.auth import UserModel
from app.backend.src.schemas.common import APIResponse, PageData, PageQuery, ok, page_data
from app.backend.src.schemas.device import (
    DeviceCreateRequest,
    DevicePublic,
    DeviceTokenResponse,
    DeviceUpdateRequest,
)
from app.backend.src.services.device_service import DeviceError
from app.backend.src.utils.auth import get_container, get_current_user, require_roles
from app.backend.src.utils.errors import to_http

router = APIRouter(prefix="/devices", tags=["devices"])

_EngineerOrAdmin = Annotated[UserModel, Depends(require_roles("engineer"))]


@router.get("", response_model=APIResponse[PageData[DevicePublic]])
async def list_devices(
    page: Annotated[PageQuery, Depends()],
    user: Annotated[UserModel, Depends(get_current_user)],
    container: Annotated[Container, Depends(get_container)],
    tenant_id: int | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    keyword: str | None = None,
) -> APIResponse:
    try:
        rows, total = await container.device_service.list(
            user,
            page=page.page,
            page_size=page.page_size,
            tenant_id=tenant_id,
            status=status_filter,
            keyword=keyword,
        )
    except DeviceError as exc:
        raise to_http(exc) from exc
    return ok(
        page_data(
            items=[DevicePublic.model_validate(row) for row in rows],
            total=total,
            page=page.page,
            page_size=page.page_size,
        )
    )


@router.post("", response_model=APIResponse[DevicePublic], status_code=status.HTTP_201_CREATED)
async def create_device(
    body: DeviceCreateRequest,
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        device = await container.device_service.create(**body.model_dump())
    except DeviceError as exc:
        raise to_http(exc) from exc
    return ok(DevicePublic.model_validate(device), message="设备已创建")


@router.get("/{device_id}", response_model=APIResponse[DevicePublic])
async def get_device(
    device_id: int,
    user: Annotated[UserModel, Depends(get_current_user)],
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        device = await container.device_service.get(user, device_id)
    except DeviceError as exc:
        raise to_http(exc) from exc
    return ok(DevicePublic.model_validate(device))


@router.put("/{device_id}", response_model=APIResponse[DevicePublic])
async def update_device(
    device_id: int,
    body: DeviceUpdateRequest,
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        device = await container.device_service.update(
            device_id, body.model_dump(exclude_unset=True, exclude_none=True)
        )
    except DeviceError as exc:
        raise to_http(exc) from exc
    return ok(DevicePublic.model_validate(device), message="设备已更新")


@router.post("/{device_id}/token", response_model=APIResponse[DeviceTokenResponse])
async def issue_device_token(
    device_id: int,
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """签发/重发设备上报令牌（工控机用 ``X-Device-Token`` 上报）。

    明文**只返回这一次**；重发会让现场正在用的旧令牌立即失效，请先与装机人员确认。
    """
    try:
        device, token = await container.device_service.issue_token(device_id)
    except DeviceError as exc:
        raise to_http(exc) from exc
    return ok(
        DeviceTokenResponse(
            device_id=device.id,
            device_code=device.device_code,
            device_token=token,
        ),
        message="令牌已签发（只显示这一次）",
    )
