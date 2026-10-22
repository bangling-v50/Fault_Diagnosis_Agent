from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.backend.src.containers import Container
from app.backend.src.models.auth import UserModel
from app.backend.src.schemas.common import APIResponse, PageData, PageQuery, ok, page_data
from app.backend.src.schemas.user import (
    PasswordResetRequest,
    UserCreateRequest,
    UserPublic,
    UserUpdateRequest,
)
from app.backend.src.services.user_service import UserError
from app.backend.src.utils.auth import get_container, require_roles
from app.backend.src.utils.errors import to_http

router = APIRouter(prefix="/users", tags=["users"])

_EngineerOrAdmin = Annotated[UserModel, Depends(require_roles("engineer"))]


@router.get("", response_model=APIResponse[PageData[UserPublic]])
async def list_users(
    page: Annotated[PageQuery, Depends()],
    actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
    tenant_id: int | None = None,
    role: str | None = Query(default=None, pattern="^(operator|engineer|admin)$"),
    status_filter: Annotated[str | None, Query(alias="status", pattern="^(active|disabled)$")] = None,
    keyword: str | None = None,
) -> APIResponse:
    rows, total = await container.user_service.list_users(
        actor,
        page=page.page,
        page_size=page.page_size,
        tenant_id=tenant_id,
        role=role,
        status=status_filter,
        keyword=keyword,
    )
    return ok(
        page_data(
            items=[UserPublic.model_validate(row) for row in rows],
            total=total,
            page=page.page,
            page_size=page.page_size,
        )
    )


@router.get("/assignable", response_model=APIResponse[list[UserPublic]])
async def list_assignable_users(
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """可接工单的人（启用状态的 engineer / admin）——工单派单的下拉数据源。

    ⚠️ 必须声明在 ``/{user_id}`` **之前**：FastAPI 按声明顺序匹配，否则 ``assignable``
    会被当成 user_id 去解析成整数，直接 422。
    """
    rows = await container.user_service.list_assignable()
    return ok([UserPublic.model_validate(row) for row in rows])


@router.post("", response_model=APIResponse[UserPublic], status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreateRequest,
    actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """建号：engineer 只能建 operator；admin 可建任意角色。"""
    try:
        user = await container.user_service.create_user(actor, **body.model_dump())
    except UserError as exc:
        raise to_http(exc) from exc
    return ok(UserPublic.model_validate(user), message="账号已创建")


@router.get("/{user_id}", response_model=APIResponse[UserPublic])
async def get_user(
    user_id: int,
    actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        user = await container.user_service.get_user(actor, user_id)
    except UserError as exc:
        raise to_http(exc) from exc
    return ok(UserPublic.model_validate(user))


@router.put("/{user_id}", response_model=APIResponse[UserPublic])
async def update_user(
    user_id: int,
    body: UserUpdateRequest,
    actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """局部更新资料/状态；role 与 tenant_id 仅 admin 可改。"""
    try:
        user = await container.user_service.update_user(
            actor, user_id, body.model_dump(exclude_unset=True, exclude_none=True)
        )
    except UserError as exc:
        raise to_http(exc) from exc
    return ok(UserPublic.model_validate(user), message="账号已更新")


@router.post("/{user_id}/password", response_model=APIResponse[None])
async def reset_password(
    user_id: int,
    body: PasswordResetRequest,
    actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """重置密码；重置后该账号所有登录会话立即失效。"""
    try:
        await container.user_service.reset_password(actor, user_id, body.new_password)
    except UserError as exc:
        raise to_http(exc) from exc
    return ok(message="密码已重置")


@router.post("/{user_id}/disable", response_model=APIResponse[UserPublic])
async def disable_user(
    user_id: int,
    actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        user = await container.user_service.set_status(actor, user_id, "disabled")
    except UserError as exc:
        raise to_http(exc) from exc
    return ok(UserPublic.model_validate(user), message="账号已停用")


@router.post("/{user_id}/enable", response_model=APIResponse[UserPublic])
async def enable_user(
    user_id: int,
    actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        user = await container.user_service.set_status(actor, user_id, "active")
    except UserError as exc:
        raise to_http(exc) from exc
    return ok(UserPublic.model_validate(user), message="账号已启用")
