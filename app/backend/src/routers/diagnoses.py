from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.backend.src.containers import Container
from app.backend.src.models import UserModel
from app.backend.src.schemas.common import APIResponse, PageData, PageQuery, ok, page_data
from app.backend.src.schemas.diagnosis import (
    DiagnosisDetail,
    DiagnosisEvidencePublic,
    DiagnosisReportPublic,
    DiagnosisStepPublic,
    DiagnosisTaskDetail,
    DiagnosisTaskPublic,
    ReportEvidenceLinkPublic,
)
from app.backend.src.services.diagnosis_service import DiagnosisError
from app.backend.src.utils.auth import get_container, get_current_user
from app.backend.src.utils.errors import to_http

router = APIRouter(prefix="/diagnoses", tags=["diagnoses"])

# 任何登录用户可达；能看哪些任务由服务层按租户裁剪
_AnyUser = Annotated[UserModel, Depends(get_current_user)]


@router.get("", response_model=APIResponse[PageData[DiagnosisTaskPublic]])
async def list_diagnoses(
    page: Annotated[PageQuery, Depends()],
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
    tenant_id: int | None = None,
    device_id: int | None = None,
    alert_id: int | None = None,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(QUEUED|RUNNING|SUCCEEDED|FAILED|CANCELLED)$")
    ] = None,
) -> APIResponse:
    """分页列出诊断任务（operator 只能看到本租户）。

    ``status`` 是 FastAPI 的保留名，所以参数名用 ``status_filter`` + ``alias``（同告警域）。
    列表项里带 ``device_code`` / ``alert_code``——由查询时的 join 带出，前端不用再回查。
    """
    try:
        rows, total = await container.diagnosis_service.list_tasks(
            actor,
            page=page.page,
            page_size=page.page_size,
            tenant_id=tenant_id,
            device_id=device_id,
            alert_id=alert_id,
            status=status_filter,
        )
    except DiagnosisError as exc:
        raise to_http(exc) from exc
    items = [
        DiagnosisTaskPublic.model_validate(task).model_copy(
            update={"device_code": device_code, "alert_code": alert_code}
        )
        for task, device_code, alert_code in rows
    ]
    return ok(page_data(items=items, total=total, page=page.page, page_size=page.page_size))


@router.get("/{task_id}", response_model=APIResponse[DiagnosisDetail])
async def get_diagnosis(
    task_id: int,
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """一个诊断任务的**完整过程**：任务头 + 全部步骤 + 全部证据 + 所有报告版本 + 引用边。

    一次给全是刻意的：回放页面靠它一个请求画完，也省得前端自己维护四个列表的加载状态。

    证据带 ``citation_no``（1 起），与报告正文里写的"依据 [2]"**同源**——前端据此把
    引用编号做成一跳就能滚到对应证据的锚点。
    """
    try:
        detail = await container.diagnosis_service.get_task_detail(actor, task_id)
    except DiagnosisError as exc:
        raise to_http(exc) from exc
    return ok(
        DiagnosisDetail(
            task=DiagnosisTaskDetail.model_validate(detail.task).model_copy(
                update={"input_snapshot": detail.task.input_payload}
            ),
            steps=[DiagnosisStepPublic.model_validate(row) for row in detail.steps],
            evidence=[
                DiagnosisEvidencePublic.model_validate(row).model_copy(
                    update={"citation_no": citation_no}
                )
                for row, citation_no in detail.evidence
            ],
            reports=[DiagnosisReportPublic.model_validate(row) for row in detail.reports],
            links=[ReportEvidenceLinkPublic.model_validate(row) for row in detail.links],
        )
    )
