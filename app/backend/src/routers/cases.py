from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi import status as http_status

from app.backend.src.containers import Container
from app.backend.src.models import UserModel
from app.backend.src.schemas.case import CaseCreateRequest, CaseCreateResult, CasePublic
from app.backend.src.schemas.common import APIResponse, PageData, PageQuery, ok, page_data
from app.backend.src.services.case_service import CaseError
from app.backend.src.utils.auth import get_container, get_current_user, require_roles
from app.backend.src.utils.errors import to_http

router = APIRouter(prefix="/cases", tags=["cases"])

_AnyUser = Annotated[UserModel, Depends(get_current_user)]
_EngineerOrAdmin = Annotated[UserModel, Depends(require_roles("engineer"))]


@router.get("", response_model=APIResponse[PageData[CasePublic]])
async def list_cases(
    page: Annotated[PageQuery, Depends()],
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
    device_model: str | None = None,
    alert_code: str | None = None,
    work_order_id: int | None = None,
) -> APIResponse:
    """分页列出可见案例。

    ``work_order_id`` 是给工单详情页用的筛选项——带上它就能判断"这张工单沉淀了没有"。
    """
    try:
        rows, total = await container.case_service.list_cases(
            actor,
            page=page.page,
            page_size=page.page_size,
            device_model=device_model,
            alert_code=alert_code,
            work_order_id=work_order_id,
        )
    except CaseError as exc:
        raise to_http(exc) from exc
    return ok(
        page_data(
            items=[CasePublic.model_validate(row) for row in rows],
            total=total,
            page=page.page,
            page_size=page.page_size,
        )
    )


@router.get("/{case_id}", response_model=APIResponse[CasePublic])
async def get_case(
    case_id: int,
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        case = await container.case_service.get_case(actor, case_id)
    except CaseError as exc:
        raise to_http(exc) from exc
    return ok(CasePublic.model_validate(case))


@router.post(
    "", response_model=APIResponse[CaseCreateResult], status_code=http_status.HTTP_201_CREATED
)
async def create_case(
    body: CaseCreateRequest,
    actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """把已结案工单沉淀为案例，并**自动生成一份知识文档触发索引**（回流）。

    重复调用是幂等的：``created=false`` 表示命中了已有案例（同一张工单，或同机型同告警码
    同根因同方案的案例已经存在），此时返回的是那条已有记录。
    """
    try:
        case, created = await container.case_service.create_case(
            actor,
            work_order_id=body.work_order_id,
            root_cause=body.root_cause,
            solution=body.solution,
            resolution_result=body.resolution_result,
            summary=body.summary,
            keywords=body.keywords,
            scope=body.scope,
        )
    except CaseError as exc:
        raise to_http(exc) from exc
    return ok(
        CaseCreateResult(case=CasePublic.model_validate(case), created=created),
        message="案例已沉淀，知识文档索引已入队" if created else "该案例已存在，未重复沉淀",
    )
