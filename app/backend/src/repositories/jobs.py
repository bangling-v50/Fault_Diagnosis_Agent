from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backend.src.models.jobs import BackgroundJobEventModel, BackgroundJobModel


class BackgroundJobRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ---------- 生产 ----------

    async def enqueue(
        self,
        *,
        kind: str,
        resource_type: str,
        resource_id: int,
        payload: dict,
        tenant_id: int | None = None,
        max_attempts: int = 3,
        timeout_seconds: int = 900,
        available_at: datetime | None = None,
    ) -> BackgroundJobModel:
        now = available_at or _utcnow()
        row = BackgroundJobModel(
            tenant_id=tenant_id,
            kind=kind,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload,
            status="QUEUED",
            attempt=0,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            available_at=now,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    # ---------- 消费 ----------

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_expires_at: datetime,
        now: datetime,
    ) -> BackgroundJobModel | None:
        claimable = or_(
            and_(
                BackgroundJobModel.status == "QUEUED",
                BackgroundJobModel.available_at <= now,
            ),
            # 租约过期的 RUNNING：进程崩溃后的恢复
            and_(
                BackgroundJobModel.status == "RUNNING",
                BackgroundJobModel.lease_expires_at < now,
            ),
        )
        stmt = (
            select(BackgroundJobModel)
            .where(claimable)
            .order_by(BackgroundJobModel.available_at, BackgroundJobModel.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        async with self._session_factory() as session:
            job = (await session.scalars(stmt)).one_or_none()
            if job is None:
                return None
            job.status = "RUNNING"
            job.lease_owner = worker_id
            job.lease_expires_at = lease_expires_at
            job.attempt = job.attempt + 1
            if job.started_at is None:
                job.started_at = now
            await session.commit()
            await session.refresh(job)
        return job

    async def renew_lease(
        self, *, job_id: int, worker_id: str, lease_expires_at: datetime
    ) -> bool:
        async with self._session_factory() as session:
            job = await session.get(BackgroundJobModel, job_id)
            if (
                job is None
                or job.lease_owner != worker_id
                or job.status != "RUNNING"
            ):
                return False
            job.lease_expires_at = lease_expires_at
            await session.commit()
        return True

    async def mark_succeeded(self, *, job_id: int, worker_id: str) -> BackgroundJobModel | None:
        return await self._finish(
            job_id=job_id,
            worker_id=worker_id,
            status="SUCCEEDED",
            event_type="result",
            payload={"status": "SUCCEEDED"},
            clear_error=True,
        )

    async def mark_cancelled(self, *, job_id: int, worker_id: str) -> BackgroundJobModel | None:
        return await self._finish(
            job_id=job_id,
            worker_id=worker_id,
            status="CANCELLED",
            event_type="status",
            payload={"status": "CANCELLED"},
        )

    async def handle_failure(
        self,
        *,
        job_id: int,
        worker_id: str,
        error_message: str,
        retry_at: datetime,
    ) -> BackgroundJobModel | None:
        """未达上限则退回 QUEUED 等待重试，否则 FAILED。"""
        async with self._session_factory() as session:
            job = await session.get(BackgroundJobModel, job_id)
            if job is None or job.lease_owner != worker_id:
                return None
            final = job.attempt >= job.max_attempts
            job.error_message = error_message
            job.lease_owner = None
            job.lease_expires_at = None
            if final:
                job.status = "FAILED"
                job.completed_at = _utcnow()
            else:
                job.status = "QUEUED"
                job.available_at = retry_at
            await session.commit()
            await session.refresh(job)
        await self.append_event(
            job_id=job_id,
            event_type="error" if final else "status",
            payload={
                "status": job.status,
                "attempt": job.attempt,
                "maxAttempts": job.max_attempts,
                "error": error_message,
            },
        )
        return job

    async def _finish(
        self,
        *,
        job_id: int,
        worker_id: str,
        status: str,
        event_type: str,
        payload: dict,
        clear_error: bool = False,
    ) -> BackgroundJobModel | None:
        async with self._session_factory() as session:
            job = await session.get(BackgroundJobModel, job_id)
            if job is None or job.lease_owner != worker_id:
                return None
            job.status = status
            job.completed_at = _utcnow()
            job.lease_owner = None
            job.lease_expires_at = None
            if clear_error:
                job.error_message = None
            await session.commit()
            await session.refresh(job)
        await self.append_event(job_id=job_id, event_type=event_type, payload=payload)
        return job

    # ---------- 事件与查询 ----------

    async def append_event(self, *, job_id: int, event_type: str, payload: dict) -> None:
        """按 job 维度串行分配 sequence，保证事件有序（回放依赖它）。"""
        async with self._session_factory() as session:
            # 锁住该 job 行，避免并发 worker/请求算出同一个 sequence
            await session.execute(
                select(BackgroundJobModel.id)
                .where(BackgroundJobModel.id == job_id)
                .with_for_update()
            )
            next_sequence = int(
                (
                    await session.scalar(
                        select(
                            func.coalesce(func.max(BackgroundJobEventModel.sequence), 0) + 1
                        ).where(BackgroundJobEventModel.job_id == job_id)
                    )
                )
                or 1
            )
            session.add(
                BackgroundJobEventModel(
                    job_id=job_id,
                    sequence=next_sequence,
                    event_type=event_type,
                    payload=payload,
                )
            )
            await session.commit()

    async def get(self, job_id: int) -> BackgroundJobModel | None:
        async with self._session_factory() as session:
            return await session.get(BackgroundJobModel, job_id)

    async def request_cancel(self, job_id: int) -> BackgroundJobModel | None:
        """QUEUED 的直接置 CANCELLED；RUNNING 的留标记，由 handler 在安全点协作退出。"""
        async with self._session_factory() as session:
            job = await session.get(BackgroundJobModel, job_id)
            if job is None:
                return None
            if job.status == "QUEUED":
                job.status = "CANCELLED"
                job.completed_at = _utcnow()
            elif job.status == "RUNNING":
                job.cancel_requested_at = _utcnow()
            await session.commit()
            await session.refresh(job)
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
        conditions = []
        if status:
            conditions.append(BackgroundJobModel.status == status)
        if kind:
            conditions.append(BackgroundJobModel.kind == kind)
        if tenant_id is not None:
            conditions.append(BackgroundJobModel.tenant_id == tenant_id)
        count_stmt = select(func.count()).select_from(BackgroundJobModel).where(*conditions)
        page_stmt = (
            select(BackgroundJobModel)
            .where(*conditions)
            .order_by(BackgroundJobModel.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        async with self._session_factory() as session:
            total = int((await session.scalar(count_stmt)) or 0)
            rows = list((await session.scalars(page_stmt)).all())
        return rows, total

    async def list_events(
        self, job_id: int, *, after_sequence: int = 0
    ) -> list[BackgroundJobEventModel]:
        stmt = (
            select(BackgroundJobEventModel)
            .where(
                BackgroundJobEventModel.job_id == job_id,
                BackgroundJobEventModel.sequence > after_sequence,
            )
            .order_by(BackgroundJobEventModel.sequence)
        )
        async with self._session_factory() as session:
            return list((await session.scalars(stmt)).all())


def _utcnow() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)
