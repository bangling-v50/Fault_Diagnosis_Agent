from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass

from app.backend.src.background_jobs.runtime import BackgroundJobContext, JobCancelled
from app.backend.src.documents import splitter
from app.backend.src.documents.errors import DocumentError
from app.backend.src.documents.extraction import SUPPORTED_EXTENSIONS, extract_text, suffix_of
from app.backend.src.documents.hierarchy import ParentChunk, chunk_document_hierarchy
from app.backend.src.documents.storage import ObjectStorage, content_hash, safe_filename
from app.backend.src.llm_provider.embedding import EmbeddingModelProvider
from app.backend.src.models import UserModel
from app.backend.src.models.jobs import BackgroundJobModel
from app.backend.src.models.knowledge import DocumentIndexTaskModel, KnowledgeDocumentModel
from app.backend.src.repositories.documents import (
    DocumentIndexTaskRepository,
    DocumentRepository,
)
from app.backend.src.repositories.tenants import TenantRepository
from app.backend.src.schemas.document import DocumentUploadForm
from app.backend.src.services.job_service import JobService
from app.backend.src.settings import Settings
from app.backend.src.vector_store.pgvector import ChunkRecord, ParentRecord, PGVectorStore

logger = logging.getLogger(__name__)

MIME_BY_SUFFIX = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
}

# 走**父子块**的文件类型：PDF 是我们自己解析成 Markdown 的，``.md`` 上传的本来就是
# Markdown——两者都能按 ``##`` 切出章节结构。其余（``.txt``）没有这个结构，仍旧按固定长度切。
HIERARCHY_SUFFIXES = frozenset({".pdf", ".md"})


@dataclass(frozen=True, slots=True)
class _ChildPlan:
    """一个待写入的子块（还没嵌入、还没落库）。"""

    content: str
    # 父块在**本文档**里的序号（不是数据库 id）——写库时才换成 id，
    # 因为父块的 id 是数据库生成的（见 `_index_document` 的 5.1/5.2 两步）。
    parent_index: int | None
    element_type: str | None
    heading_path: str | None


# 按文件类型选择切分方式，统一产出 (父块列表, 子块列表)
def _split_for_index(
    text: str, *, filename: str, settings: Settings
) -> tuple[list[ParentChunk], list[_ChildPlan]]:
    if suffix_of(filename) in HIERARCHY_SUFFIXES:
        # 父块列表
        tree = chunk_document_hierarchy(text)
        return tree, [
            _ChildPlan(
                content=child.content,
                parent_index=parent.index,
                element_type=child.element_type,
                heading_path=child.heading_path,
            )
            for parent in tree
            for child in parent.children
        ]
    # 纯文本文件，都是子块没有父块，使用之前的固定长度字符切分
    chunks = splitter.chunk_document_text(
        text,
        strategy=settings.doc_chunk_strategy,
        chunk_size=settings.doc_chunk_size,
        chunk_overlap=settings.doc_chunk_overlap,
    )
    return [], [
        _ChildPlan(
            content=chunk.content,
            parent_index=None,
            element_type=None,
            heading_path=chunk.heading_path,
        )
        for chunk in chunks
    ]


def _split_summary(
    parents: list[ParentChunk], children: list[_ChildPlan], *, settings: Settings
) -> str:
    """给任务事件写一句人话——索引跑完时一眼看出"切成了什么"。"""
    if parents:
        kinds = Counter(child.element_type for child in children)
        return (
            f"按章节切分为 {len(parents)} 个父块、{len(children)} 个子块"
            f"（段落 {kinds.get('paragraph', 0)} / 表头 {kinds.get('table_head', 0)}"
            f" / 表行 {kinds.get('table_row', 0)}）"
        )
    return (
        f"切分为 {len(children)} 个片段（strategy={settings.doc_chunk_strategy},"
        f" size={settings.doc_chunk_size}, overlap={settings.doc_chunk_overlap}）"
    )


class DocumentService:
    def __init__(
        self,
        *,
        document_repo: DocumentRepository,
        index_task_repo: DocumentIndexTaskRepository,
        tenant_repo: TenantRepository,
        job_service: JobService,
        vector_store: PGVectorStore,
        storage: ObjectStorage,
        max_upload_bytes: int,
    ) -> None:
        self._document_repo = document_repo
        self._index_task_repo = index_task_repo
        self._tenant_repo = tenant_repo
        self._job_service = job_service
        # 删除文档时要清掉它的向量，所以这里也持有向量库（仅 delete 用得到）
        self._vector_store = vector_store
        # 文档实体在对象存储里（MinIO / OSS / S3 同一套代码）；库里 file_path 存的是对象 key
        self._storage = storage
        self._max_upload_bytes = max_upload_bytes

    # ---------- 上传 ----------
    async def upload(
        self,
        actor: UserModel,
        *,
        filename: str,
        content: bytes,
        form: DocumentUploadForm,
    ) -> tuple[KnowledgeDocumentModel, DocumentIndexTaskModel, BackgroundJobModel]:
        """上传一份文档并触发索引，返回 ``(文档, 索引任务, 后台任务)``。

        步骤（顺序不可换）：校验后缀/大小/租户 → 内容去重 → 落盘 → 落库 →
        建索引任务 → 入队 → 回填 ``background_job_id``。
        """
        # 1) 后缀：与抽取层共用同一个判断，避免两处规则不一致
        suffix = suffix_of(filename)
        if suffix not in SUPPORTED_EXTENSIONS:
            supported = "、".join(sorted(SUPPORTED_EXTENSIONS))
            raise DocumentError(
                "UNSUPPORTED_FILE_TYPE",
                f"不支持的文件类型「{suffix or '无扩展名'}」，仅支持 {supported}",
            )

        # 2) 大小
        if len(content) > self._max_upload_bytes:
            limit_mb = self._max_upload_bytes / 1024 / 1024
            raise DocumentError(
                "DOCUMENT_TOO_LARGE",
                f"文件超过上限 {limit_mb:.0f}MB（当前 {len(content) / 1024 / 1024:.1f}MB）",
            )

        # 3) 租户（global 文档不需要）
        tenant_id = await self._resolve_tenant_id(form)

        # 4) 内容去重：同 scope/租户下同一份文件只留一份
        digest = content_hash(content)
        existing = await self._document_repo.find_by_content_hash(
            digest, scope=form.scope, tenant_id=tenant_id
        )
        if existing is not None:
            raise DocumentError(
                "DOCUMENT_DUPLICATE",
                f"相同内容的文档已存在：{existing.filename}（id={existing.id}）",
            )

        # 5) 写入对象存储（同步 IO → 线程池）
        file_path, digest = await asyncio.to_thread(
            self._storage.save_file, form.scope, tenant_id, filename, content, digest
        )

        # 6) 落库
        document = await self._document_repo.create(
            tenant_id=tenant_id,
            scope=form.scope,
            filename=safe_filename(filename),
            doc_name=form.doc_name or filename,
            doc_type=form.doc_type,
            version=form.version,
            size_bytes=len(content),
            mime_type=MIME_BY_SUFFIX.get(suffix),
            file_path=file_path,
            content_hash=digest,
            created_by=actor.id,
        )

        # 7) 建索引任务 + 入队（与「重新索引 / 重试」共用同一段）
        index_task, job = await self._schedule_index(document=document)
        return document, index_task, job

    async def create_from_text(
        self,
        actor: UserModel,
        *,
        filename: str,
        text: str,
        form: DocumentUploadForm,
        source_type: str = "system",
        source_id: int | None = None,
    ) -> tuple[KnowledgeDocumentModel, DocumentIndexTaskModel, BackgroundJobModel]:
        """从一段**文本**建文档（不是用户上传的文件），走同样的落盘 → 落库 → 建索引任务 → 入队。

        目前唯一的调用方是**案例沉淀**（``services/case_service.py``）。

        与 :meth:`upload` 的两点差别，都是刻意的：

        1. ``source_type`` / ``source_id`` 由调用方指定——``knowledge_documents.source_type``
           的 CHECK 允许 ``upload`` / ``diagnosis_case`` / ``system``，案例要标成
           ``diagnosis_case`` 并指向来源工单；
        2. **跳过内容去重**。去重是按 ``(content_hash, scope, tenant_id)`` 判的，与文档来源无关，
           所以"同一份手册被两个租户各自上传"会互撞；案例这边更不该靠它兜底——案例的唯一性由
           ``diagnosis_cases.case_fingerprint`` 保证（见 ``case_service``）。

        仍然保留后缀与大小校验：正文最终是要落成真文件的（``file_path`` 是 NOT NULL）。
        """
        content = text.encode("utf-8")
        suffix = suffix_of(filename)
        if suffix not in SUPPORTED_EXTENSIONS:
            supported = "、".join(sorted(SUPPORTED_EXTENSIONS))
            raise DocumentError(
                "UNSUPPORTED_FILE_TYPE",
                f"不支持的文件类型「{suffix or '无扩展名'}」，仅支持 {supported}",
            )
        if len(content) > self._max_upload_bytes:
            limit_mb = self._max_upload_bytes / 1024 / 1024
            raise DocumentError(
                "DOCUMENT_TOO_LARGE",
                f"内容超过上限 {limit_mb:.0f}MB（当前 {len(content) / 1024 / 1024:.1f}MB）",
            )

        tenant_id = await self._resolve_tenant_id(form)
        digest = content_hash(content)
        file_path, digest = await asyncio.to_thread(
            self._storage.save_file, form.scope, tenant_id, filename, content, digest
        )
        document = await self._document_repo.create(
            tenant_id=tenant_id,
            scope=form.scope,
            filename=safe_filename(filename),
            doc_name=form.doc_name or filename,
            doc_type=form.doc_type,
            version=form.version,
            size_bytes=len(content),
            mime_type=MIME_BY_SUFFIX.get(suffix),
            file_path=file_path,
            content_hash=digest,
            created_by=actor.id,
            source_type=source_type,
            source_id=source_id,
        )
        index_task, job = await self._schedule_index(document=document)
        return document, index_task, job

    # ---------- 查询（可见性规则：global 人人可见，租户文档仅本租户） ----------

    async def get_document(self, actor: UserModel, document_id: int) -> KnowledgeDocumentModel:
        document = await self._document_repo.find_by_id(document_id)
        if document is None:
            raise DocumentError("DOCUMENT_NOT_FOUND", f"文档不存在：id={document_id}")
        self._ensure_visible(actor, document)
        return document

    async def list_documents(
        self,
        actor: UserModel,
        *,
        page: int,
        page_size: int,
        tenant_id: int | None = None,
        scope: str | None = None,
        doc_type: str | None = None,
        index_status: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
    ) -> tuple[list[KnowledgeDocumentModel], int]:
        """分页列出可见文档。

        operator 被**强制**限定在自己租户（并把 global 文档一并带上）；engineer/admin
        可跨租户，`tenant_id` 只是可选的筛选条件。
        """
        # 默认只看未删除的；想看已删除的显式传 status='deleted'
        status = status or "active"
        if actor.role == "operator":
            if actor.tenant_id is None:
                raise DocumentError("FORBIDDEN", "账号未绑定租户，无法查看文档")
            return await self._document_repo.list(
                page=page,
                page_size=page_size,
                tenant_id=actor.tenant_id,
                include_global=True,
                scope=scope,
                doc_type=doc_type,
                index_status=index_status,
                status=status,
                keyword=keyword,
            )
        return await self._document_repo.list(
            page=page,
            page_size=page_size,
            tenant_id=tenant_id,
            include_global=tenant_id is not None,  # 筛某租户时把 global 也算上
            scope=scope,
            doc_type=doc_type,
            index_status=index_status,
            status=status,
            keyword=keyword,
        )

    # ---------- 删除 / 重新索引 ----------

    async def delete_document(self, actor: UserModel, document_id: int) -> KnowledgeDocumentModel:
        """软删文档：置 status='deleted'，并**清掉它的向量**。

        顺序很关键：**先清向量、再软删行**。反过来会出现"行已不可见、但片段还能被检索到"
        的脏数据——已删文档的内容仍被 RAG 召回，这属于数据泄露。
        """
        document = await self.get_document(actor, document_id)
        if document.status == "deleted":
            return document  # 幂等
        await self._vector_store.delete_document_content(document.id)
        # 原文件**保留**：软删不等于销毁，将来要恢复还得靠它（清理另写脚本）
        return await self._document_repo.soft_delete(document.id) or document

    async def reindex(
        self, actor: UserModel, document_id: int, *, retry_of_task_id: int | None = None
    ) -> tuple[KnowledgeDocumentModel, DocumentIndexTaskModel, BackgroundJobModel]:
        """重新索引：再建一条索引任务并入队（旧向量由索引服务在写入前清掉）。"""
        document = await self.get_document(actor, document_id)
        if document.status != "active":
            raise DocumentError(
                "DOCUMENT_NOT_INDEXABLE", f"文档当前状态为 {document.status}，不能索引"
            )
        await self._document_repo.update(document.id, {"index_status": "pending"})
        index_task, job = await self._schedule_index(
            document=document, retry_of_task_id=retry_of_task_id
        )
        return document, index_task, job

    async def retry_index_task(
        self, actor: UserModel, index_task_id: int
    ) -> tuple[KnowledgeDocumentModel, DocumentIndexTaskModel, BackgroundJobModel]:
        """重试某个失败的索引任务：新建一条任务（`retry_of_task_id` 指回原任务）。"""
        previous = await self._index_task_repo.find_by_id(index_task_id)
        if previous is None:
            raise DocumentError("INDEX_TASK_NOT_FOUND", f"索引任务不存在：id={index_task_id}")
        return await self.reindex(actor, previous.document_id, retry_of_task_id=previous.id)

    # ---------- 索引任务查询 ----------
    async def get_index_task(self, index_task_id: int) -> DocumentIndexTaskModel:
        task = await self._index_task_repo.find_by_id(index_task_id)
        if task is None:
            raise DocumentError("INDEX_TASK_NOT_FOUND", f"索引任务不存在：id={index_task_id}")
        return task

    async def list_index_tasks(
        self,
        *,
        page: int,
        page_size: int,
        document_id: int | None = None,
        tenant_id: int | None = None,
        status: str | None = None,
    ) -> tuple[list[DocumentIndexTaskModel], int]:
        return await self._index_task_repo.list(
            page=page,
            page_size=page_size,
            document_id=document_id,
            tenant_id=tenant_id,
            status=status,
        )

    # ---------- 内部 ----------
    # 上传、重新索引、重试都用这一段——**入队与建任务绑在一起**，杜绝"建了任务忘了入队"，那种永远停在 QUEUED 的孤儿任务。
    async def _schedule_index(
        self, *, document: KnowledgeDocumentModel, retry_of_task_id: int | None = None
    ) -> tuple[DocumentIndexTaskModel, BackgroundJobModel]:
        # 创建索引任务
        index_task = await self._index_task_repo.create_index_task(
            tenant_id=document.tenant_id,
            document_id=document.id,
            retry_of_task_id=retry_of_task_id,
        )
        # 进入后台队伍排队
        job = await self._job_service.enqueue(
            kind="document_index",
            resource_type="document",
            resource_id=document.id,
            tenant_id=document.tenant_id,
            payload={
                "documentId": document.id,
                "indexTaskId": index_task.id,
                "filename": document.filename,
            },
        )
        # 更新索引任务信息
        index_task = (
            await self._index_task_repo.set_background_job(index_task.id, job.id) or index_task
        )
        return index_task, job

    def _ensure_visible(self, actor: UserModel, document: KnowledgeDocumentModel) -> None:
        """global 文档人人可见；租户文档只有本租户的人能看（engineer/admin 不受限）。"""
        if actor.role == "operator" and document.scope != "global" and (
            document.tenant_id != actor.tenant_id
        ):
            raise DocumentError("FORBIDDEN", "无权查看其他租户的文档")

    async def _resolve_tenant_id(self, form: DocumentUploadForm) -> int | None:
        """global → None；tenant → 校验租户存在且启用后返回其 id。"""
        if form.scope == "global":
            return None
        if form.tenant_id is None:  # 表单校验已保证非空；这里同时收窄类型
            raise DocumentError("DOCUMENT_SCOPE_INVALID", "scope='tenant' 时必须提供 tenant_id")
        tenant = await self._tenant_repo.find_by_id(form.tenant_id)
        if tenant is None or tenant.status != "active":
            raise DocumentError("TENANT_NOT_FOUND", "租户不存在或未启用")
        return tenant.id


class DocumentIndexingService:
    """worker 侧：把一个索引任务真正跑完。

    与 ``DocumentService`` 不同，这个服务跑在**后台 worker** 里，入口收
    ``BackgroundJobContext``——靠它可以写进度事件（前端能回放）并响应协作式取消
    （长 PDF 的抽取 + 几百次 embedding 中途可以被打断）。
    """

    def __init__(
        self,
        *,
        document_repo: DocumentRepository,
        index_task_repo: DocumentIndexTaskRepository,
        vector_store: PGVectorStore,
        embedding_model: EmbeddingModelProvider,
        storage: ObjectStorage,
        settings: Settings,
    ) -> None:
        self._document_repo = document_repo
        self._index_task_repo = index_task_repo
        self._vector_store = vector_store
        self._embedding_model = embedding_model
        # 索引时要读回原文件（对象存储），还要在旁边写抽取副产物
        self._storage = storage
        self._settings = settings

    async def run_task(self, context: BackgroundJobContext, *, task_id: int) -> None:
        """执行一次索引任务。

        **异常一律向上抛**（不吞）：runtime 据此决定退避重试还是终态 FAILED；
        同时这里已经把 `document.index_status` 与索引任务状态落成 failed，
        所以"任务失败了"这件事在业务侧也看得见，而不是只有 job 表知道。
        """
        # 获取对应索引任务
        task = await self._index_task_repo.find_by_id(task_id)
        if task is None:
            raise DocumentError("INDEX_TASK_NOT_FOUND", f"索引任务不存在：id={task_id}")

        # 获取数据库中，对应文档的元数据
        document = await self._document_repo.find_by_id(task.document_id)
        if document is None:
            raise DocumentError("DOCUMENT_NOT_FOUND", f"文档不存在：id={task.document_id}")
        if document.status != "active":
            raise DocumentError(
                "DOCUMENT_NOT_INDEXABLE", f"文档当前状态为 {document.status}，不予索引"
            )

        await self._index_task_repo.mark_running(task.id)
        await self._document_repo.update(document.id, {"index_status": "indexing"})
        await context.append_event(
            {"step": "start", "message": f"开始索引「{document.filename}」（{document.scope}）"}
        )

        try:
            chunk_count = await self._index_document(context, document)
        except JobCancelled:
            # 索引任务表没有 CANCELLED 状态（CHECK 只允许 QUEUED/RUNNING/SUCCEEDED/FAILED），
            # 所以取消记成 FAILED 并写明原因；后台任务本身由 runtime 记成 CANCELLED。
            await self._mark_failed(task.id, document.id, "任务已被取消")
            raise
        except Exception as exc:
            await self._mark_failed(task.id, document.id, f"{type(exc).__name__}: {exc}")
            raise  # 交给 runtime：未达上限会退避重试，达到上限则 job 也终态失败

        await self._index_task_repo.mark_succeeded(task.id)
        await self._document_repo.update(document.id, {"index_status": "indexed"})
        await context.append_event(
            {"step": "done", "message": f"索引完成：{chunk_count} 个片段已写入向量库"}
        )

    # ---------- 内部 ----------
    # 真正索引工作
    async def _index_document(
        self, context: BackgroundJobContext, document: KnowledgeDocumentModel
    ) -> int:
        """抽取 → 切分 → 嵌入 → 写向量库；返回写入的片段数。"""
        settings = self._settings

        # 1) 从对象存储读回原文件（同步 IO → 线程池）。文件不在会抛 DOCUMENT_FILE_MISSING。
        raw = await asyncio.to_thread(self._storage.read_file, document.file_path)
        await context.raise_if_cancelled()

        # 2) 抽取纯文本（CPU 型 → 线程池）
        # pdf是把内容抽成 md 返回
        # md就是直接解码返回
        text = await asyncio.to_thread(extract_text, raw, filename=document.filename)
        if not text.strip():
            raise DocumentError(
                "DOCUMENT_NOT_INDEXABLE",
                "该文件抽不出文本（可能是扫描件或纯图片 PDF），无法索引",
            )
        await context.append_event({"step": "extract", "message": f"抽取到 {len(text)} 个字符"})

        # 2.5) 顺手把抽取结果落一份到原文件旁，供人工检查解析质量（PDF 转出来的
        #      Markdown 长什么样、表格有没有拼对，打开看一眼就知道）。
        #      纯旁路：它写不进去也不影响索引——索引读的是内存里这份 text。
        await self._write_sidecar(document, text)

        # 3) 切分：Markdown 来源的（PDF / md）走 父子块 ，其余仍按固定长度
        parents, children = _split_for_index(
            text, filename=document.filename, settings=settings
        )
        if not children:
            raise DocumentError("DOCUMENT_NOT_INDEXABLE", "切分后没有得到任何片段")
        await context.append_event(
            {"step": "split", "message": _split_summary(parents, children, settings=settings)}
        )

        # 4) 嵌入：**只嵌子块**。父块不参与召回，没有向量可生成——这正是它单独一张表、
        #    不带 embedding 列的原因。自己分批（而不是一次性交给底层），
        #    这样每批之间能报进度、能响应取消。
        vectors: list[list[float]] = []
        batch_size = max(1, settings.embedding_batch_size)
        for start in range(0, len(children), batch_size):
            await context.raise_if_cancelled()
            part = children[start : start + batch_size]
            vectors.extend(
                await self._embedding_model.aembed_documents([item.content for item in part])
            )
            done = min(start + batch_size, len(children))
            await context.append_event(
                {"step": "embed", "message": f"已嵌入 {done}/{len(children)} 个片段"}
            )
        if len(vectors) != len(children):
            raise DocumentError(
                "DOCUMENT_NOT_INDEXABLE",
                f"嵌入结果与片段数不一致：{len(vectors)} vs {len(children)}",
            )

        # 5) 写库。**先清掉该文档的旧内容**——新片段数比旧的少时，只靠
        #    "同 (document_id, chunk_index) 覆盖"会留下旧的高位片段（Milvus 时代实测过；
        #    pgvector 这边 ON CONFLICT 也只在同一序号上覆盖，同样需要先删）。
        await self._vector_store.delete_document_content(document.id)

        # 5.1) **先写父块**：子块的 parent_id 要指向它们，而 id 是数据库生成的，
        #      所以得插完拿回来（save_parents 用 RETURNING 做这件事）。
        parent_ids = await self._vector_store.save_parents(
            [
                ParentRecord(
                    document_id=document.id,
                    parent_index=parent.index,
                    heading_path=parent.heading_path,
                    content=parent.content,
                    scope=document.scope,
                    tenant_id=document.tenant_id,
                )
                for parent in parents
            ]
        )

        # 5.2) 再写子块。chunk_index 是**扁平连续序号**（跨父块一路递增），
        #      保证"同一文档同一序号只有一条"，ON CONFLICT 才能正确覆盖。
        records = [
            ChunkRecord(
                document_id=document.id,
                chunk_index=index,
                content=child.content,
                embedding=vector,
                scope=document.scope,
                tenant_id=document.tenant_id,
                parent_id=(
                    parent_ids.get(child.parent_index)
                    if child.parent_index is not None
                    else None
                ),
                element_type=child.element_type,
                heading_path=child.heading_path,
            )
            for index, (child, vector) in enumerate(zip(children, vectors, strict=True))
        ]
        written = await self._vector_store.save_chunks(records)
        await context.append_event(
            {
                "step": "upsert",
                "message": f"已写入 {len(parent_ids)} 个父块、{written} 个子块",
            }
        )
        return written


    async def _write_sidecar(self, document: KnowledgeDocumentModel, text: str) -> None:
        """把抽取文本落一份到原文件旁（供人工检查）。**失败只记日志。**

        它是给人看的副产物——解析出来的 Markdown 长什么样、表格有没有拼对，打开看一眼
        就知道，不用去翻库里的片段。所以这里吞掉所有异常：磁盘满了、权限不对，
        都不该让整个索引任务连带失败。

        **只有 PDF 需要**：``.md`` / ``.txt`` 的原文件本身就是可读文本，抽出来跟原文一样，
        再存一份纯属重复。
        """
        if suffix_of(document.filename) != ".pdf":
            return
        try:
            await asyncio.to_thread(self._storage.write_sidecar, document.file_path, text)
        except Exception as exc:  # noqa: BLE001 —— 旁路，绝不外抛
            logger.warning("抽取副产物写入失败（不影响索引）：%s", exc)

    async def _mark_failed(self, task_id: int, document_id: int, reason: str) -> None:
        """失败/取消时把两处状态都落下来（业务侧也要看得见失败）。"""
        await self._index_task_repo.mark_failed(task_id, reason)
        await self._document_repo.update(document_id, {"index_status": "failed"})
