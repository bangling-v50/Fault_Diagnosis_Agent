from __future__ import annotations

from datetime import datetime

from sqlalchemy.exc import IntegrityError

from app.backend.src.models.auth import UserModel
from app.backend.src.models.equipment import DeviceModel
from app.backend.src.repositories.devices import DeviceRepository
from app.backend.src.repositories.tenants import TenantRepository
from app.backend.src.utils.tokens import DEVICE_TOKEN_PREFIX, generate_token, hash_token

VALID_STATUS = ("online", "offline", "disabled")


class DeviceError(Exception):
    """携带机器可读错误码的领域异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DeviceService:
    def __init__(
        self,
        *,
        device_repo: DeviceRepository,
        tenant_repo: TenantRepository,
    ) -> None:
        self._device_repo = device_repo
        self._tenant_repo = tenant_repo

    # ---------- 写 ----------

    async def create(
        self,
        *,
        tenant_id: int,
        device_code: str,
        device_name: str | None = None,
        device_model: str | None = None,
        serial_number: str | None = None,
        install_location: str | None = None,
        install_date: datetime | None = None,
    ) -> DeviceModel:
        tenant = await self._tenant_repo.find_by_id(tenant_id)
        if tenant is None or tenant.status != "active":
            raise DeviceError("TENANT_NOT_FOUND", "租户不存在或未启用")

        code = device_code.strip()
        if await self._device_repo.find_by_code(code) is not None:
            raise DeviceError("DEVICE_CODE_TAKEN", "设备编码已存在")
        try:
            return await self._device_repo.create(
                tenant_id=tenant.id,
                device_code=code,
                device_name=device_name,
                device_model=device_model,
                serial_number=serial_number,
                install_location=install_location,
                install_date=install_date,
            )
        except IntegrityError as exc:
            raise DeviceError("DEVICE_CODE_TAKEN", "设备编码已存在") from exc

    async def update(self, device_id: int, fields: dict) -> DeviceModel:
        if "status" in fields and fields["status"] not in VALID_STATUS:
            raise DeviceError("INVALID_STATUS", "设备状态只能是 online / offline / disabled")
        updated = await self._device_repo.update(device_id, fields)
        if updated is None:
            raise DeviceError("DEVICE_NOT_FOUND", "设备不存在")
        return updated

    # ---------- 读（带租户作用域） ----------

    async def get(self, actor: UserModel, device_id: int) -> DeviceModel:
        device = await self._device_repo.find_by_id(device_id)
        if device is None:
            raise DeviceError("DEVICE_NOT_FOUND", "设备不存在")
        self._ensure_visible(actor, device)
        return device

    async def list(
        self,
        actor: UserModel,
        *,
        page: int,
        page_size: int,
        tenant_id: int | None = None,
        status: str | None = None,
        keyword: str | None = None,
    ) -> tuple[list[DeviceModel], int]:
        scoped_tenant_id = self._scope_tenant(actor, tenant_id)
        return await self._device_repo.list(
            page=page,
            page_size=page_size,
            tenant_id=scoped_tenant_id,
            status=status,
            keyword=keyword,
        )

    # ---------- 上报令牌（设备鉴权） ----------

    async def issue_token(self, device_id: int) -> tuple[DeviceModel, str]:
        """签发/重发设备上报令牌。

        **明文只在此刻返回一次**（库里只存哈希）；重发会让旧令牌立刻失效。
        """
        device = await self._device_repo.find_by_id(device_id)
        if device is None:
            raise DeviceError("DEVICE_NOT_FOUND", "设备不存在")
        if device.status == "disabled":
            raise DeviceError("DEVICE_DISABLED", "设备已停用，不能签发上报令牌")

        token = generate_token(DEVICE_TOKEN_PREFIX)
        updated = await self._device_repo.set_token_hash(device.id, hash_token(token))
        if updated is None:
            raise DeviceError("DEVICE_NOT_FOUND", "设备不存在")
        return updated, token

    async def authenticate_token(self, token: str) -> DeviceModel:
        """按上报令牌还原设备（供 ``get_current_device`` 依赖使用）。

        失败一律抛同一种错误，不区分"令牌不存在""设备已停用""租户已停用"，
        避免把这里当成探测设备/租户是否存在的接口。
        """
        invalid = DeviceError("DEVICE_UNAUTHENTICATED", "设备令牌无效")
        if not token or not token.strip():
            raise DeviceError("DEVICE_UNAUTHENTICATED", "缺少设备令牌")

        device = await self._device_repo.find_by_token_hash(hash_token(token.strip()))
        if device is None or device.status == "disabled":
            raise invalid
        tenant = await self._tenant_repo.find_by_id(device.tenant_id)
        if tenant is None or tenant.status != "active":
            raise invalid
        return device

    # ---------- 内部 ----------

    def _scope_tenant(self, actor: UserModel, requested_tenant_id: int | None) -> int | None:
        if actor.role == "operator":
            if actor.tenant_id is None:
                raise DeviceError("FORBIDDEN", "账号未绑定租户，无法查看设备")
            if requested_tenant_id is not None and requested_tenant_id != actor.tenant_id:
                raise DeviceError("FORBIDDEN", "无权查看其他租户的设备")
            return actor.tenant_id
        return requested_tenant_id

    def _ensure_visible(self, actor: UserModel, device: DeviceModel) -> None:
        if actor.role == "operator" and device.tenant_id != actor.tenant_id:
            raise DeviceError("FORBIDDEN", "无权查看其他租户的设备")
