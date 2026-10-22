from __future__ import annotations

from app.backend.src.models.jobs import BackgroundJobEventModel, BackgroundJobModel
from app.backend.src.repositories.jobs import BackgroundJobRepository

# 白名单。加新 kind 时三处要一起改：这里、models/jobs.py 的 CHECK、以及一次 ALTER
VALID_KINDS = ("diagnosis", "document_index", "chat_memory_extract", "multimodal_index")
VALID_RESOURCE_TYPES = ("diagnosis_task", "document", "chat_session")


class JobError(Exception):
    """携带机器可读错误码的领域异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class JobService:
    def __init__(self, *, job_repo: BackgroundJobRepository) -> None:
        self._job_repo = job_repo

    async def enqueue(
        self,
        *,
        kind: str,
        resource_type: str,
        resource_id: int,
        payload: dict | None = None,
        tenant_id: int | None = None,
        max_attempts: int = 3,
        timeout_seconds: int = 900,
    ) -> BackgroundJobModel:
        if kind not in VALID_KINDS:
            raise JobError("INVALID_JOB_KIND", f"任务类型只能是 {VALID_KINDS}")
        if resource_type not in VALID_RESOURCE_TYPES:
            raise JobError("INVALID_RESOURCE_TYPE", f"资源类型只能是 {VALID_RESOURCE_TYPES}")
        return await self._job_repo.enqueue(
            kind=kind,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload or {},
            tenant_id=tenant_id,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
        )

    async def get(self, job_id: int) -> BackgroundJobModel:
        job = await self._job_repo.get(job_id)
        if job is None:
            raise JobError("JOB_NOT_FOUND", "后台任务不存在")
        return job

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
        kind: str | None = None,
        tenant_id: int | None = None,
    ) -> tuple[list[BackgroundJobModel], int]:
        return await self._job_repo.list(
            page=page, page_size=page_size, status=status, kind=kind, tenant_id=tenant_id
        )

    async def cancel(self, job_id: int) -> BackgroundJobModel:
        job = await self._job_repo.request_cancel(job_id)
        if job is None:
            raise JobError("JOB_NOT_FOUND", "后台任务不存在")
        return job

    async def events(self, job_id: int, *, after_sequence: int = 0) -> list[BackgroundJobEventModel]:
        await self.get(job_id)  # 不存在则 404
        return await self._job_repo.list_events(job_id, after_sequence=after_sequence)
