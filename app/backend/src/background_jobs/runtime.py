from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TypeAlias
from uuid import uuid4

from app.backend.src.models.jobs import BackgroundJobModel
from app.backend.src.repositories.jobs import BackgroundJobRepository

logger = logging.getLogger(__name__)

# handler 的签名：拿一个窄上下文，干完返回；抛异常即视为失败，由 runtime 决定重试还是终态
JobHandler: TypeAlias = Callable[["BackgroundJobContext"], Awaitable[None]]
# 退避上限：2**attempt 涨得快，封顶 30 秒，免得任务被推到很远的将来
MAX_BACKOFF_SECONDS = 30


# 协作式取消信号：handler 在安全点抛它，runtime 捕获后把任务记成 CANCELLED
class JobCancelled(RuntimeError):
    pass


# 交给 handler 的窄上下文：只暴露"当前任务 + 追加事件 + 检查取消"，不给它数据库权限
@dataclass(frozen=True, slots=True)
class BackgroundJobContext:
    job: BackgroundJobModel
    repository: BackgroundJobRepository

    # 往 background_job_events 追加一条事件（按 job 串行编号），进度回放靠它
    async def append_event(self, payload: dict, event_type: str = "progress") -> None:
        await self.repository.append_event(
            job_id=self.job.id, event_type=event_type, payload=payload
        )

    # 安全点检查：重新读一次任务行，发现任务没了或有取消标记就抛 JobCancelled
    async def raise_if_cancelled(self) -> None:
        current = await self.repository.get(self.job.id)
        if current is None or current.cancel_requested_at is not None:
            raise JobCancelled(f"后台任务 {self.job.id} 被请求取消")


# 运行时本体：N 个 worker 协程循环"领取 → 执行"；租约 + 心跳保证同一任务同时只有一个 worker 在跑
class BackgroundJobRuntime:
    # 只保存配置与 handler 注册表；不在这里建连接（仓储自带 session_factory）
    def __init__(
        self,
        repository: BackgroundJobRepository,
        *,
        concurrency: int = 2,
        lease_seconds: int = 30,
        poll_seconds: float = 0.5,
    ) -> None:
        self._repository = repository
        self._concurrency = max(1, concurrency)
        self._lease_seconds = max(6, lease_seconds)
        self._poll_seconds = max(0.05, poll_seconds)
        self._handlers: dict[str, JobHandler] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        self._runtime_id = f"runtime_{uuid4().hex}"

    # 把某种任务类型绑到处理函数上；重复注册直接报错，让配置错误在启动时就暴露
    def register(self, kind: str, handler: JobHandler) -> None:
        if kind in self._handlers:
            raise ValueError(f"后台任务 handler 重复注册：{kind}")
        self._handlers[kind] = handler

    # 启动 concurrency 个 worker 协程；幂等——已经启动过就直接返回
    async def start(self) -> None:
        if self._workers:
            return
        self._stopping.clear()
        self._workers = [
            asyncio.create_task(self._worker_loop(index), name=f"bg-worker-{index}")
            for index in range(self._concurrency)
        ]

    # 停止：置停止标志 + cancel 所有 worker 并等它们退出，保证进程能干净关闭
    async def stop(self) -> None:
        self._stopping.set()
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    # 单个 worker 的主循环：领到任务就执行，领不到就睡一小会再试（轮询式领取）
    async def _worker_loop(self, index: int) -> None:
        worker_id = f"{self._runtime_id}:{index}"
        while not self._stopping.is_set():
            now = _utc_now()
            job = await self._repository.claim_next(
                worker_id=worker_id,
                lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                now=now,
            )
            if job is None:
                await asyncio.sleep(self._poll_seconds)
                continue
            await self._execute(job, worker_id)

    # 执行一个已领取的任务：找 handler → 起心跳 → 限时执行 → 成功/取消/失败三条收尾 → 停心跳
    async def _execute(self, job: BackgroundJobModel, worker_id: str) -> None:
        handler = self._handlers.get(job.kind)         # 拿到的是 build_document_index_handler 返回的内层 handle
        if handler is None:
            await self._repository.handle_failure(
                job_id=job.id,
                worker_id=worker_id,
                error_message=f"没有为 kind={job.kind} 注册 handler",
                retry_at=_utc_now(),
            )
            return

        logger.info("background.job.started job=%s kind=%s", job.id, job.kind)
        context = BackgroundJobContext(job=job, repository=self._repository)
        heartbeat = asyncio.create_task(self._heartbeat(job.id, worker_id))
        try:
            await context.raise_if_cancelled()
            await asyncio.wait_for(handler(context), timeout=job.timeout_seconds)
            await context.raise_if_cancelled()
        except JobCancelled:
            await self._repository.mark_cancelled(job_id=job.id, worker_id=worker_id)
            logger.info("background.job.cancelled job=%s", job.id)
        except Exception as exc:  # noqa: BLE001 —— 任何 handler 异常都要落库并决定重试
            retry_at = _utc_now() + timedelta(seconds=min(MAX_BACKOFF_SECONDS, 2**job.attempt))
            updated = await self._repository.handle_failure(
                job_id=job.id,
                worker_id=worker_id,
                error_message=_safe_error(exc),
                retry_at=retry_at,
            )
            logger.warning(
                "background.job.failed job=%s final=%s error=%s",
                job.id,
                updated is not None and updated.status == "FAILED",
                exc.__class__.__name__,
            )
        else:
            await self._repository.mark_succeeded(job_id=job.id, worker_id=worker_id)
            logger.info("background.job.completed job=%s", job.id)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    # 心跳：每"租约 1/3"续租一次，让任务不被别人抢走；续不上（已被回收）就退出
    async def _heartbeat(self, job_id: int, worker_id: str) -> None:
        interval = self._lease_seconds / 3
        while not self._stopping.is_set():
            await asyncio.sleep(interval)
            renewed = await self._repository.renew_lease(
                job_id=job_id,
                worker_id=worker_id,
                lease_expires_at=_utc_now() + timedelta(seconds=self._lease_seconds),
            )
            if not renewed:
                return


# 把异常压成一行可入库的文本（超时单独给中文提示），并截断，避免超长错误信息撑爆列
def _safe_error(error: Exception) -> str:
    if isinstance(error, asyncio.TimeoutError):
        return "后台任务执行超时"
    text = str(error).strip()
    return (text or error.__class__.__name__)[:1000]


# 统一用带时区的 UTC 时间——库列是 timestamptz，混用 naive datetime 会报错
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
