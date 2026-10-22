from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.backend.src.containers import Container
from app.backend.src.models.auth import UserModel
from app.backend.src.models.equipment import DeviceModel
from app.backend.src.services.auth_service import AuthError
from app.backend.src.services.device_service import DeviceError
from app.backend.src.utils.errors import DOMAIN_STATUS

_bearer = HTTPBearer(auto_error=False)

# 工控机上报用的设备令牌；与用户登录态完全分离（工控机没有用户账号）
_device_token = APIKeyHeader(name="X-Device-Token", auto_error=False)


def get_container(request: Request) -> Container:
    return request.app.state.container


def _credentials_error(message: str, code: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    container: Container = Depends(get_container),
) -> UserModel:
    if credentials is None or not credentials.credentials:
        raise _credentials_error("缺少登录凭证", "AUTH_UNAUTHENTICATED")
    try:
        return await container.auth_service.authenticate(credentials.credentials)
    except AuthError as exc:
        http_status = DOMAIN_STATUS.get(exc.code, status.HTTP_401_UNAUTHORIZED)
        headers = (
            {"WWW-Authenticate": "Bearer"}
            if http_status == status.HTTP_401_UNAUTHORIZED
            else None
        )
        raise HTTPException(
            status_code=http_status,
            detail={"code": exc.code, "message": exc.message},
            headers=headers,
        ) from exc


def require_roles(*roles: str):
    """角色门禁依赖工厂：``Depends(require_roles("engineer"))``。``admin`` 直接放行。"""

    async def checker(user: Annotated[UserModel, Depends(get_current_user)]) -> UserModel:
        if user.role == "admin" or user.role in roles:
            return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "AUTH_FORBIDDEN", "message": "权限不足"},
        )

    return checker


async def get_current_device(
    token: Annotated[str | None, Depends(_device_token)] = None,
    container: Container = Depends(get_container),
) -> DeviceModel:
    """设备上报鉴权：从 ``X-Device-Token`` 头还原设备（**不依赖用户登录态**）。

    失败一律 401 且不说明具体原因（不区分"令牌不存在/设备停用/租户停用"），
    避免这个端点变成探测设备是否存在的接口。
    """
    try:
        return await container.device_service.authenticate_token(token or "")
    except DeviceError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
