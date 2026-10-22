from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DeviceStatus = Literal["online", "offline", "disabled"]


class DeviceCreateRequest(BaseModel):
    tenant_id: int
    device_code: str = Field(min_length=1, max_length=128)
    device_name: str | None = Field(default=None, max_length=255)
    device_model: str | None = Field(default=None, max_length=128)
    serial_number: str | None = Field(default=None, max_length=128)
    install_location: str | None = Field(default=None, max_length=255)
    install_date: datetime | None = None


class DeviceUpdateRequest(BaseModel):
    """局部更新：只提交要改的字段（None 表示不改）。"""

    device_name: str | None = Field(default=None, max_length=255)
    device_model: str | None = Field(default=None, max_length=128)
    serial_number: str | None = Field(default=None, max_length=128)
    install_location: str | None = Field(default=None, max_length=255)
    install_date: datetime | None = None
    status: DeviceStatus | None = None


class DevicePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int
    device_code: str
    device_name: str | None
    device_model: str | None
    serial_number: str | None
    install_location: str | None
    install_date: datetime | None
    status: str
    last_online_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DeviceTokenResponse(BaseModel):
    """设备上报令牌的签发结果。

    ``device_token`` 是**明文，只在签发这一刻返回一次**——库里只存哈希，
    关掉就再也查不回来，只能重新签发（届时旧令牌立刻失效）。
    """

    device_id: int
    device_code: str
    device_token: str
    token_type: str = "device"
    note: str = "请立即交给现场装机人员写入工控机：关闭后无法再次查看，只能重新签发（旧令牌会立即失效）"

