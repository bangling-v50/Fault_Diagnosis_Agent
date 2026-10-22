from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pwdlib import PasswordHash

from app.backend.src.models.auth import UserModel
from app.backend.src.repositories.auth_sessions import AuthSessionRepository
from app.backend.src.repositories.tenants import TenantRepository
from app.backend.src.repositories.users import UserRepository
from app.backend.src.utils.tokens import generate_token, hash_token


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuthError(Exception):
    """携带机器可读错误码的领域异常；由路由/依赖层映射成 HTTP 状态码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AuthResult:
    access_token: str
    expires_in: int
    user: UserModel


class AuthService:
    def __init__(
        self,
        *,
        user_repo: UserRepository,
        tenant_repo: TenantRepository,
        session_repo: AuthSessionRepository,
        password_hash: PasswordHash | None = None,
        session_ttl_seconds: int = 7 * 24 * 3600,
    ) -> None:
        self._user_repo = user_repo
        self._tenant_repo = tenant_repo
        self._session_repo = session_repo
        self._password_hash = password_hash or PasswordHash.recommended()
        self._session_ttl_seconds = session_ttl_seconds
        # 兜底：用户不存在时也对同样的开销执行一次 verify，降低用户名枚举的时序差
        self._dummy_hash = self._password_hash.hash("dummy-password-for-timing")

    # ---------- 对外流程 ----------

    async def login(self, *, username: str, password: str) -> AuthResult:
        user = await self._user_repo.find_by_username(username.strip())
        if user is None:
            self._password_hash.verify(password, self._dummy_hash)  # 维持等开销
            raise AuthError("INVALID_CREDENTIALS", "用户名或密码错误")
        if not self._password_hash.verify(password, user.password_hash):
            raise AuthError("INVALID_CREDENTIALS", "用户名或密码错误")
        if user.status != "active":
            raise AuthError("ACCOUNT_DISABLED", "账号已停用，请联系管理员")
        await self._ensure_tenant_active(user)
        return await self._open_session(user)

    async def logout(self, token: str) -> None:
        await self._session_repo.revoke(hash_token(token), _utcnow())

    async def authenticate(self, token: str) -> UserModel:
        """按不透明 token 还原当前用户；会话不存在/过期/已撤销/账号或租户被停用都视为不可用。"""
        now = _utcnow()
        session = await self._session_repo.find_valid_by_token_hash(hash_token(token), now)
        if session is None:
            raise AuthError("AUTH_UNAUTHENTICATED", "登录已失效，请重新登录")
        user = await self._user_repo.find_by_id(session.user_id)
        if user is None or user.status != "active":
            await self._session_repo.revoke(hash_token(token), now)
            raise AuthError("AUTH_UNAUTHENTICATED", "登录已失效，请重新登录")
        await self._ensure_tenant_active(user)
        await self._session_repo.update_last_seen(session.id, now)
        return user

    # ---------- 内部辅助 ----------

    async def _ensure_tenant_active(self, user: UserModel) -> None:
        """租户被停用后，其下所有账号（含已登录会话）都不应继续使用。"""
        if user.tenant_id is None:
            return
        tenant = await self._tenant_repo.find_by_id(user.tenant_id)
        if tenant is None or tenant.status != "active":
            raise AuthError("TENANT_INACTIVE", "所属租户已停用，请联系售后")

    async def _open_session(self, user: UserModel) -> AuthResult:
        token = generate_token()
        now = _utcnow()
        await self._session_repo.create(
            user_id=user.id,
            token_hash=hash_token(token),
            last_seen_at=now,
            expires_at=now + timedelta(seconds=self._session_ttl_seconds),
        )
        return AuthResult(access_token=token, expires_in=self._session_ttl_seconds, user=user)
