from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TenantStatus = Literal["active", "inactive"]


class TenantCreateRequest(BaseModel):
    tenant_code: str = Field(min_length=1, max_length=64)
    tenant_name: str = Field(min_length=1, max_length=255)
    region: str | None = Field(default=None, max_length=128)
    address: str | None = None
    contact_person: str | None = Field(default=None, max_length=128)
    contact_phone: str | None = Field(default=None, max_length=64)


class TenantUpdateRequest(BaseModel):
    """局部更新：只提交要改的字段（None 表示不改）。"""

    tenant_name: str | None = Field(default=None, min_length=1, max_length=255)
    region: str | None = Field(default=None, max_length=128)
    address: str | None = None
    contact_person: str | None = Field(default=None, max_length=128)
    contact_phone: str | None = Field(default=None, max_length=64)
    status: TenantStatus | None = None


class TenantPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_code: str
    tenant_name: str
    region: str | None
    address: str | None
    contact_person: str | None
    contact_phone: str | None
    status: str
    created_at: datetime
    updated_at: datetime
