from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["operator", "engineer", "admin"]
UserStatus = Literal["active", "disabled"]


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    real_name: str | None
    email: str | None
    role: str
    tenant_id: int | None
    status: str
    created_at: datetime


class UserCreateRequest(BaseModel):
    """建号请求。engineer 只能建 operator；admin 可建任意角色。"""

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=128)
    role: Role = "operator"
    tenant_id: int | None = None  # 建 operator 时必填
    real_name: str | None = Field(default=None, max_length=128)
    email: str | None = Field(default=None, max_length=320)


class UserUpdateRequest(BaseModel):
    """局部更新：只提交要改的字段（None 表示不改）。"""

    real_name: str | None = Field(default=None, max_length=128)
    email: str | None = Field(default=None, max_length=320)
    status: UserStatus | None = None
    role: Role | None = None  # 仅 admin 可改
    tenant_id: int | None = None  # 仅当把角色改为 operator 时用于指定租户


class PasswordResetRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)
