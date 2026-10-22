from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from app.backend.src.models.auth import TenantModel
from app.backend.src.repositories.tenants import TenantRepository

VALID_STATUS = ("active", "inactive")


class TenantError(Exception):
    """携带机器可读错误码的领域异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TenantService:
    def __init__(self, *, tenant_repo: TenantRepository) -> None:
        self._tenant_repo = tenant_repo

    async def create(
        self,
        *,
        tenant_code: str,
        tenant_name: str,
        region: str | None = None,
        address: str | None = None,
        contact_person: str | None = None,
        contact_phone: str | None = None,
    ) -> TenantModel:
        code = tenant_code.strip()
        if await self._tenant_repo.find_by_code(code) is not None:
            raise TenantError("TENANT_CODE_TAKEN", "租户编码已存在")
        try:
            return await self._tenant_repo.create(
                tenant_code=code,
                tenant_name=tenant_name.strip(),
                region=region,
                address=address,
                contact_person=contact_person,
                contact_phone=contact_phone,
            )
        except IntegrityError as exc:  # 并发下的兜底
            raise TenantError("TENANT_CODE_TAKEN", "租户编码已存在") from exc

    async def get(self, tenant_id: int) -> TenantModel:
        tenant = await self._tenant_repo.find_by_id(tenant_id)
        if tenant is None:
            raise TenantError("TENANT_NOT_FOUND", "租户不存在")
        return tenant

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
        keyword: str | None = None,
    ) -> tuple[list[TenantModel], int]:
        return await self._tenant_repo.list(
            page=page, page_size=page_size, status=status, keyword=keyword
        )

    async def update(self, tenant_id: int, fields: dict) -> TenantModel:
        if "status" in fields and fields["status"] not in VALID_STATUS:
            raise TenantError("INVALID_STATUS", "状态只能是 active 或 inactive")
        tenant = await self._tenant_repo.update(tenant_id, fields)
        if tenant is None:
            raise TenantError("TENANT_NOT_FOUND", "租户不存在")
        return tenant

    async def set_status(self, tenant_id: int, status: str) -> TenantModel:
        return await self.update(tenant_id, {"status": status})
