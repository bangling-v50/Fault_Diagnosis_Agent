from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 与 knowledge_documents 的 CHECK 约束保持一致（见 models/knowledge.py）
DocScope = Literal["global", "tenant"]
DocType = Literal["manual", "sop", "diagnostic_case", "faq"]


class DocumentUploadForm(BaseModel):
    """上传表单字段（不含文件本体）。

    路由里**逐个用 ``Form()`` 声明**这些字段，再组装成本模型传给服务——
    实测当前 FastAPI/Pydantic 版本下 ``Annotated[DocumentUploadForm, Form()]`` 不会把字段
    铺平（它会找一个名为 ``form`` 的整字段），所以不用那种写法。

    "``scope='tenant'`` 必须给 ``tenant_id``" 这条跨字段规则在**服务层**保证
    （``DocumentService._resolve_tenant_id`` 抛 ``DOCUMENT_SCOPE_INVALID``），
    这里不重复放 ``model_validator``——规则只留一处。
    """

    scope: DocScope = Field(default="tenant", description="global=全公司通用；tenant=某租户专属")
    tenant_id: int | None = Field(default=None, description="scope='tenant' 时必填")
    doc_type: DocType = Field(default="manual", description="manual / sop / diagnostic_case / faq")
    doc_name: str | None = Field(default=None, max_length=255, description="展示用名称，缺省用文件名")
    version: str | None = Field(default=None, max_length=64, description="文档版本")


class DocumentIndexRequest(BaseModel):
    """重新索引某个文档的请求体（POST /documents/index_document）。"""

    document_id: int


class DocumentPublic(BaseModel):
    """文档详情 / 列表项（**不含 file_path**）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int | None
    scope: str
    filename: str
    doc_name: str | None
    doc_type: str
    version: str | None
    size_bytes: int | None
    mime_type: str | None
    content_hash: str | None
    status: str
    index_status: str
    source_type: str
    created_by: int | None
    created_at: datetime
    updated_at: datetime


class DocumentIndexTaskPublic(BaseModel):
    """索引任务详情 / 列表项。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int | None
    document_id: int
    background_job_id: int | None
    status: str
    retry_of_task_id: int | None
    failure_reason: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class DocumentUploadResult(BaseModel):
    """上传响应：文档已落盘落库，索引任务已入队。

    ``job_id`` 可用于到 ``/jobs/{id}`` 追进度与事件（QUEUED → RUNNING → SUCCEEDED/FAILED）。
    """

    document: DocumentPublic
    index_task: DocumentIndexTaskPublic
    job_id: int
