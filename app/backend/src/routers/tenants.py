from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.backend.src.containers import Container
from app.backend.src.models.auth import UserModel
from app.backend.src.schemas.common import APIResponse, PageData, PageQuery, ok, page_data
from app.backend.src.schemas.tenant import (
    TenantCreateRequest,
    TenantPublic,
    TenantUpdateRequest,
)
from app.backend.src.services.tenant_service import TenantError
from app.backend.src.utils.auth import get_container, require_roles
from app.backend.src.utils.errors import to_http

router = APIRouter(prefix="/tenants", tags=["tenants"])

# engineer 可管；admin 由 require_roles 内部放行
_EngineerOrAdmin = Annotated[UserModel, Depends(require_roles("engineer"))]


@router.get("", response_model=APIResponse[PageData[TenantPublic]])
async def list_tenants(
    page: Annotated[PageQuery, Depends()],
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    keyword: str | None = None,
) -> APIResponse:
    rows, total = await container.tenant_service.list(
        page=page.page,
        page_size=page.page_size,
        status=status_filter,
        keyword=keyword,
    )
    return ok(
        page_data(
            items=[TenantPublic.model_validate(row) for row in rows],
            total=total,
            page=page.page,
            page_size=page.page_size,
        )
    )


@router.post(
    "", response_model=APIResponse[TenantPublic], status_code=status.HTTP_201_CREATED
)
async def create_tenant(
    body: TenantCreateRequest,
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        tenant = await container.tenant_service.create(**body.model_dump())
    except TenantError as exc:
        raise to_http(exc) from exc
    return ok(TenantPublic.model_validate(tenant), message="租户已创建")


@router.get("/{tenant_id}", response_model=APIResponse[TenantPublic])
async def get_tenant(
    tenant_id: int,
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        tenant = await container.tenant_service.get(tenant_id)
    except TenantError as exc:
        raise to_http(exc) from exc
    return ok(TenantPublic.model_validate(tenant))


@router.put("/{tenant_id}", response_model=APIResponse[TenantPublic])
async def update_tenant(
    tenant_id: int,
    body: TenantUpdateRequest,
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        tenant = await container.tenant_service.update(
            tenant_id, body.model_dump(exclude_unset=True, exclude_none=True)
        )
    except TenantError as exc:
        raise to_http(exc) from exc
    return ok(TenantPublic.model_validate(tenant), message="租户已更新")


@router.post("/{tenant_id}/deactivate", response_model=APIResponse[TenantPublic])
async def deactivate_tenant(
    tenant_id: int,
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """停用租户（软删）；停用后该租户下账号无法登录、已登录会话也失效。"""
    try:
        tenant = await container.tenant_service.set_status(tenant_id, "inactive")
    except TenantError as exc:
        raise to_http(exc) from exc
    return ok(TenantPublic.model_validate(tenant), message="租户已停用")


@router.post("/{tenant_id}/activate", response_model=APIResponse[TenantPublic])
async def activate_tenant(
    tenant_id: int,
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        tenant = await container.tenant_service.set_status(tenant_id, "active")
    except TenantError as exc:
        raise to_http(exc) from exc
    return ok(TenantPublic.model_validate(tenant), message="租户已启用")
