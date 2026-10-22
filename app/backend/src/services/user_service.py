from __future__ import annotations

from datetime import datetime, timezone

from pwdlib import PasswordHash
from sqlalchemy.exc import IntegrityError

from app.backend.src.models.auth import UserModel
from app.backend.src.repositories.auth_sessions import AuthSessionRepository
from app.backend.src.repositories.tenants import TenantRepository
from app.backend.src.repositories.users import UserRepository

VALID_ROLES = ("operator", "engineer", "admin")
VALID_STATUS = ("active", "disabled")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserError(Exception):
    """携带机器可读错误码的领域异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class UserService:
    def __init__(
        self,
        *,
        user_repo: UserRepository,
        tenant_repo: TenantRepository,
        session_repo: AuthSessionRepository,
        password_hash: PasswordHash | None = None,
    ) -> None:
        self._user_repo = user_repo
        self._tenant_repo = tenant_repo
        self._session_repo = session_repo
        self._password_hash = password_hash or PasswordHash.recommended()

    # ---------- 建号 ----------

    async def create_user(
        self,
        actor: UserModel,
        *,
        username: str,
        password: str,
        role: str = "operator",
        tenant_id: int | None = None,
        real_name: str | None = None,
        email: str | None = None,
    ) -> UserModel:
        if role not in VALID_ROLES:
            raise UserError("ROLE_NOT_ALLOWED", "角色不合法")
        if getattr(actor, "role", None) != "admin" and role != "operator":
            raise UserError("FORBIDDEN", "无权创建该角色的账号")

        username = username.strip()
        if await self._user_repo.find_by_username(username) is not None:
            raise UserError("USERNAME_TAKEN", "用户名已存在")
        resolved_tenant_id = await self._resolve_tenant_id(role, tenant_id)

        try:
            return await self._user_repo.create(
                tenant_id=resolved_tenant_id,
                username=username,
                password_hash=self._password_hash.hash(password),
                real_name=real_name,
                email=email,
                role=role,
            )
        except IntegrityError as exc:
            raise UserError("USERNAME_TAKEN", "用户名或邮箱已被占用") from exc

    # ---------- 查询 ----------

    async def list_assignable(self) -> list[UserModel]:
        """可接工单的人：启用状态的 engineer / admin。

        为什么不复用 :meth:`list_users`：那个方法对 engineer 强制只看 operator（权限矩阵里
        "engineer 只能建管 operator"），于是工程师**列不出同事**来派单。工单需要的是
        "谁能接单"的名单，与"谁能被管理"是两回事，所以单独开一条只读通道。
        """
        rows, _ = await self._user_repo.list(page=1, page_size=100, status="active")
        return [row for row in rows if row.role in ("engineer", "admin")]

    async def list_users(
        self,
        actor: UserModel,
        *,
        page: int,
        page_size: int,
        tenant_id: int | None = None,
        role: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
    ) -> tuple[list[UserModel], int]:
        if getattr(actor, "role", None) != "admin":
            role = "operator"  # engineer 只能看到 operator
        return await self._user_repo.list(
            page=page,
            page_size=page_size,
            tenant_id=tenant_id,
            role=role,
            status=status,
            keyword=keyword,
        )

    async def get_user(self, actor: UserModel, user_id: int) -> UserModel:
        user = await self._user_repo.find_by_id(user_id)
        if user is None:
            raise UserError("USER_NOT_FOUND", "用户不存在")
        self._ensure_manageable(actor, user)
        return user

    # ---------- 修改 ----------

    async def update_user(self, actor: UserModel, user_id: int, fields: dict) -> UserModel:
        target = await self.get_user(actor, user_id)  # 含"能管谁"校验
        is_admin = getattr(actor, "role", None) == "admin"

        submitted = {key: value for key, value in fields.items() if value is not None}
        if "role" in submitted and not is_admin:
            raise UserError("FORBIDDEN", "仅管理员可以调整角色")
        allowed = (
            {"real_name", "email", "status", "role", "tenant_id"}
            if is_admin
            else {"real_name", "email", "status"}
        )
        clean = {key: value for key, value in submitted.items() if key in allowed}
        if not clean:
            return target
        if "status" in clean and clean["status"] not in VALID_STATUS:
            raise UserError("INVALID_STATUS", "状态只能是 active 或 disabled")
        if user_id == getattr(actor, "id", None) and ("status" in clean or "role" in clean):
            raise UserError("SELF_OPERATION_FORBIDDEN", "不能停用或修改自己的角色")

        if "role" in clean and clean["role"] != target.role:
            desired_tenant = clean.get("tenant_id", target.tenant_id)
            clean["tenant_id"] = await self._resolve_tenant_id(clean["role"], desired_tenant)
        elif "tenant_id" in clean:
            clean["tenant_id"] = await self._resolve_tenant_id(target.role, clean["tenant_id"])

        updated = await self._user_repo.update(user_id, clean)
        if updated is None:
            raise UserError("USER_NOT_FOUND", "用户不存在")
        if clean.get("status") == "disabled":
            await self._session_repo.revoke_all_for_user(user_id, _utcnow())
        return updated

    async def set_status(self, actor: UserModel, user_id: int, status: str) -> UserModel:
        return await self.update_user(actor, user_id, {"status": status})

    async def reset_password(self, actor: UserModel, user_id: int, new_password: str) -> None:
        target = await self.get_user(actor, user_id)
        await self._user_repo.set_password_hash(
            target.id, self._password_hash.hash(new_password)
        )
        await self._session_repo.revoke_all_for_user(target.id, _utcnow())

    async def update_self(
        self,
        actor: UserModel,
        *,
        real_name: str | None = None,
        email: str | None = None,
    ) -> UserModel:
        """自助修改本人资料（real_name / email），不带"能管谁"限制。"""
        fields = {
            key: value
            for key, value in {"real_name": real_name, "email": email}.items()
            if value is not None
        }
        if fields:
            updated = await self._user_repo.update(actor.id, fields)
        else:
            updated = await self._user_repo.find_by_id(actor.id)
        if updated is None:
            raise UserError("USER_NOT_FOUND", "用户不存在")
        return updated

    # ---------- 内部 ----------

    def _ensure_manageable(self, actor: UserModel, target: UserModel) -> None:
        if getattr(actor, "role", None) == "admin":
            return
        if target.role != "operator":
            raise UserError("FORBIDDEN", "无权管理该账号")

    async def _resolve_tenant_id(self, role: str, tenant_id: int | None) -> int | None:
        """operator 必须绑定存在且启用的租户；其余角色的 tenant_id 恒为空。"""
        if role != "operator":
            return None
        if tenant_id is None:
            raise UserError("TENANT_REQUIRED", "操作员必须绑定租户")
        tenant = await self._tenant_repo.find_by_id(tenant_id)
        if tenant is None or tenant.status != "active":
            raise UserError("TENANT_NOT_FOUND", "租户不存在或未启用")
        return tenant.id
