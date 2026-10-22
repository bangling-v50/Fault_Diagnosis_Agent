from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.backend.src.containers import Container
from app.backend.src.models.auth import UserModel
from app.backend.src.schemas.auth import AuthResponse, LoginRequest, SelfUpdateRequest
from app.backend.src.schemas.common import APIResponse, ok
from app.backend.src.schemas.user import UserPublic
from app.backend.src.services.auth_service import AuthError, AuthResult
from app.backend.src.services.user_service import UserError
from app.backend.src.utils.auth import get_container, get_current_user
from app.backend.src.utils.errors import to_http

router = APIRouter(prefix="/auth", tags=["auth"])

_bearer = HTTPBearer(auto_error=False)


def _auth_payload(result: AuthResult) -> AuthResponse:
    return AuthResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
        user=UserPublic.model_validate(result.user),
    )


@router.post("/login", response_model=APIResponse[AuthResponse])
async def login(
    body: LoginRequest,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        result = await container.auth_service.login(username=body.username, password=body.password)
    except AuthError as exc:
        raise to_http(exc) from exc
    return ok(_auth_payload(result), message="登录成功")


@router.post("/logout")
async def logout(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    _user: Annotated[UserModel, Depends(get_current_user)] = None,  # 确保 token 有效
    container: Annotated[Container, Depends(get_container)] = None,
) -> APIResponse:
    """登出当前 token：撤销该会话（前端应同时丢弃本地 token）。"""
    token = credentials.credentials if credentials is not None else ""
    await container.auth_service.logout(token)
    return ok(message="已退出登录")


@router.get("/me", response_model=APIResponse[UserPublic])
async def me(user: Annotated[UserModel, Depends(get_current_user)]) -> APIResponse:
    return ok(UserPublic.model_validate(user))


@router.put("/me", response_model=APIResponse[UserPublic])
async def update_me(
    body: SelfUpdateRequest,
    user: Annotated[UserModel, Depends(get_current_user)],
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """自助修改本人资料（real_name / email），角色与租户不可自助改。"""
    try:
        updated = await container.user_service.update_self(
            user, real_name=body.real_name, email=body.email
        )
    except UserError as exc:
        raise to_http(exc) from exc
    return ok(UserPublic.model_validate(updated), message="资料已更新")
