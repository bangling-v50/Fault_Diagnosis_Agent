from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.backend.src.containers import Container
from app.backend.src.models.auth import UserModel
from app.backend.src.schemas.common import APIResponse, PageData, PageQuery, ok, page_data
from app.backend.src.schemas.job import JobEventPublic, JobPublic
from app.backend.src.services.job_service import JobError
from app.backend.src.utils.auth import get_container, require_roles
from app.backend.src.utils.errors import to_http

router = APIRouter(prefix="/jobs", tags=["jobs"])

_EngineerOrAdmin = Annotated[UserModel, Depends(require_roles("engineer"))]


@router.get("", response_model=APIResponse[PageData[JobPublic]])
async def list_jobs(
    page: Annotated[PageQuery, Depends()],
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    kind: str | None = None,
    tenant_id: int | None = None,
) -> APIResponse:
    rows, total = await container.job_service.list(
        page=page.page,
        page_size=page.page_size,
        status=status_filter,
        kind=kind,
        tenant_id=tenant_id,
    )
    return ok(
        page_data(
            items=[JobPublic.model_validate(row) for row in rows],
            total=total,
            page=page.page,
            page_size=page.page_size,
        )
    )


@router.get("/{job_id}", response_model=APIResponse[JobPublic])
async def get_job(
    job_id: int,
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        job = await container.job_service.get(job_id)
    except JobError as exc:
        raise to_http(exc) from exc
    return ok(JobPublic.model_validate(job))


@router.get("/{job_id}/events", response_model=APIResponse[list[JobEventPublic]])
async def list_job_events(
    job_id: int,
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
    after_sequence: int = Query(default=0, ge=0),
) -> APIResponse:
    """按 sequence 拉取任务事件（增量轮询 / 回放）。"""
    try:
        events = await container.job_service.events(job_id, after_sequence=after_sequence)
    except JobError as exc:
        raise to_http(exc) from exc
    return ok([JobEventPublic.model_validate(event) for event in events])


@router.post("/{job_id}/cancel", response_model=APIResponse[JobPublic])
async def cancel_job(
    job_id: int,
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """取消任务：排队中的直接置取消，执行中的留标记由 handler 在安全点退出。"""
    try:
        job = await container.job_service.cancel(job_id)
    except JobError as exc:
        raise to_http(exc) from exc
    return ok(JobPublic.model_validate(job), message="已请求取消")
