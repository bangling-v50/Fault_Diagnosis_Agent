from __future__ import annotations

from pydantic import BaseModel, Field

from app.backend.src.schemas.user import UserPublic

__all__ = ["AuthResponse", "LoginRequest", "SelfUpdateRequest", "UserPublic"]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=128)


class SelfUpdateRequest(BaseModel):
    """PUT /auth/me：自助修改本人资料（角色 / 租户 / 状态不可自助修改）。"""

    real_name: str | None = Field(default=None, max_length=128)
    email: str | None = Field(default=None, max_length=320)


class AuthResponse(BaseModel):
    """登录成功：不透明 bearer token + 用户信息。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int  # 秒
    user: UserPublic
