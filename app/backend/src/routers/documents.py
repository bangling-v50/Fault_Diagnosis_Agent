from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status

from app.backend.src.containers import Container
from app.backend.src.documents.errors import DocumentError
from app.backend.src.models import UserModel
from app.backend.src.schemas.common import APIResponse, PageData, PageQuery, ok, page_data
from app.backend.src.schemas.document import (
    DocScope,
    DocType,
    DocumentIndexRequest,
    DocumentIndexTaskPublic,
    DocumentPublic,
    DocumentUploadForm,
    DocumentUploadResult,
)
from app.backend.src.utils.auth import get_container, get_current_user, require_roles
from app.backend.src.utils.errors import to_http

router = APIRouter(prefix="/documents", tags=["documents"])

# 需登录即可（可见性在服务层按作用域过滤）
_AnyUser = Annotated[UserModel, Depends(get_current_user)]
# 仅售后技术人员与管理员
_EngineerOrAdmin = Annotated[UserModel, Depends(require_roles("engineer"))]


def _to_upload_result(document, index_task, job) -> DocumentUploadResult:
    """把 (文档, 索引任务, 后台任务) 组装成响应体。上传与重新索引共用。"""
    return DocumentUploadResult(
        document=DocumentPublic.model_validate(document),
        index_task=DocumentIndexTaskPublic.model_validate(index_task),
        job_id=job.id,
    )


@router.post("/upload", response_model=APIResponse[DocumentUploadResult], status_code=status.HTTP_201_CREATED)
async def upload_document(
    actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
    file: Annotated[UploadFile, File()],
    scope: Annotated[DocScope, Form()] = "tenant",
    tenant_id: Annotated[int | None, Form()] = None,
    doc_type: Annotated[DocType, Form()] = "manual",
    doc_name: Annotated[str | None, Form(max_length=255)] = None,
    version: Annotated[str | None, Form(max_length=64)] = None,
) -> APIResponse:
    """上传文档：落盘 → 落库 → 建索引任务 → 入队（**上传即自动索引**）。

    支持 `.md` / `.txt` / `.pdf`（大小上限见配置）。返回的 `job_id` 可到 `/jobs/{id}` 追进度。

    表单字段**逐个声明**（类型别名复用 Schema 里的 `DocScope`/`DocType`），再组装成
    ``DocumentUploadForm`` 交给服务——不用 ``Annotated[表单模型, Form()]``，原因见 Schema 的注释。
    """
    content = await file.read()  # 上限 20MB，直接读进内存没问题
    form = DocumentUploadForm(
        scope=scope, tenant_id=tenant_id, doc_type=doc_type, doc_name=doc_name, version=version
    )
    try:
        document, index_task, job = await container.document_service.upload(
            actor, filename=file.filename or "unnamed", content=content, form=form
        )
    except DocumentError as exc:
        raise to_http(exc) from exc
    return ok(_to_upload_result(document, index_task, job), message="文档已上传，索引任务已入队")


@router.get("/get_documents", response_model=APIResponse[PageData[DocumentPublic]])
async def get_documents(
    page: Annotated[PageQuery, Depends()],
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
    tenant_id: int | None = None,
    scope: Annotated[str | None, Query(pattern="^(global|tenant)$")] = None,
    doc_type: Annotated[str | None, Query(pattern="^(manual|sop|diagnostic_case|faq)$")] = None,
    index_status: Annotated[str | None, Query(pattern="^(pending|indexing|indexed|failed)$")] = None,
    status_filter: Annotated[str | None, Query(alias="status", pattern="^(active|deleted)$")] = None,
    keyword: str | None = None,
) -> APIResponse:
    """分页列出可见文档（operator 只能看到本租户 + global 的文档）。"""
    try:
        rows, total = await container.document_service.list_documents(
            actor,
            page=page.page,
            page_size=page.page_size,
            tenant_id=tenant_id,
            scope=scope,
            doc_type=doc_type,
            index_status=index_status,
            status=status_filter,
            keyword=keyword,
        )
    except DocumentError as exc:
        raise to_http(exc) from exc
    return ok(
        page_data(
            items=[DocumentPublic.model_validate(row) for row in rows],
            total=total,
            page=page.page,
            page_size=page.page_size,
        )
    )


@router.get("/get_document/{document_id}", response_model=APIResponse[DocumentPublic])
async def get_document(
    document_id: int,
    actor: _AnyUser,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        document = await container.document_service.get_document(actor, document_id)
    except DocumentError as exc:
        raise to_http(exc) from exc
    return ok(DocumentPublic.model_validate(document))


# ---------------- 以下仅售后技术人员 / 管理员 ----------------


@router.post("/index_document", response_model=APIResponse[DocumentUploadResult])
async def index_document(
    body: DocumentIndexRequest,
    actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """重新索引指定文档（会新建一条索引任务并入队）。"""
    try:
        document, index_task, job = await container.document_service.reindex(
            actor, body.document_id
        )
    except DocumentError as exc:
        raise to_http(exc) from exc
    return ok(_to_upload_result(document, index_task, job), message="已提交重新索引")


@router.delete("/delete_document/{document_id}", response_model=APIResponse[DocumentPublic])
async def delete_document(
    document_id: int,
    actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """软删文档并清掉它的向量（原文件保留在磁盘，便于将来恢复/排查）。"""
    try:
        document = await container.document_service.delete_document(actor, document_id)
    except DocumentError as exc:
        raise to_http(exc) from exc
    return ok(DocumentPublic.model_validate(document), message="文档已删除")


@router.get("/get_index_tasks", response_model=APIResponse[PageData[DocumentIndexTaskPublic]])
async def get_index_tasks(
    page: Annotated[PageQuery, Depends()],
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
    document_id: int | None = None,
    tenant_id: int | None = None,
    status_filter: Annotated[str | None, Query(alias="status", pattern="^(QUEUED|RUNNING|SUCCEEDED|FAILED)$")] = None,
) -> APIResponse:
    """分页列出索引任务（失败原因写在 `failure_reason`，可用下面的重试接口重跑）。"""
    rows, total = await container.document_service.list_index_tasks(
        page=page.page,
        page_size=page.page_size,
        document_id=document_id,
        tenant_id=tenant_id,
        status=status_filter,
    )
    return ok(
        page_data(
            items=[DocumentIndexTaskPublic.model_validate(row) for row in rows],
            total=total,
            page=page.page,
            page_size=page.page_size,
        )
    )


@router.get("/get_index_task/{task_id}", response_model=APIResponse[DocumentIndexTaskPublic])
async def get_index_task(
    task_id: int,
    _actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    try:
        task = await container.document_service.get_index_task(task_id)
    except DocumentError as exc:
        raise to_http(exc) from exc
    return ok(DocumentIndexTaskPublic.model_validate(task))


@router.post("/index_task_retry/{index_task_id}", response_model=APIResponse[DocumentUploadResult])
async def index_task_retry(
    index_task_id: int,
    actor: _EngineerOrAdmin,
    container: Annotated[Container, Depends(get_container)],
) -> APIResponse:
    """重试一个失败的索引任务：新建任务（`retry_of_task_id` 指回原任务）并重新入队。"""
    try:
        document, index_task, job = await container.document_service.retry_index_task(
            actor, index_task_id
        )
    except DocumentError as exc:
        raise to_http(exc) from exc
    return ok(_to_upload_result(document, index_task, job), message="已提交重试")
