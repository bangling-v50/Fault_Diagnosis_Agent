from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backend.src.models.knowledge import DocumentIndexTaskModel, KnowledgeDocumentModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DocumentRepository:
    """knowledge_documents 表。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        tenant_id: int | None,
        scope: str,
        filename: str,
        doc_name: str | None,
        doc_type: str,
        version: str | None,
        size_bytes: int | None,
        mime_type: str | None,
        file_path: str,
        content_hash: str | None,
        created_by: int | None,
        source_type: str = "upload",
        source_id: int | None = None,
        status: str = "active",
        index_status: str = "pending",
    ) -> KnowledgeDocumentModel:
        row = KnowledgeDocumentModel(
            tenant_id=tenant_id,
            scope=scope,
            filename=filename,
            doc_name=doc_name,
            doc_type=doc_type,
            version=version,
            size_bytes=size_bytes,
            mime_type=mime_type,
            file_path=file_path,
            content_hash=content_hash,
            status=status,
            index_status=index_status,
            source_type=source_type,
            source_id=source_id,
            created_by=created_by,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
        return row

    async def find_by_id(self, document_id: int) -> KnowledgeDocumentModel | None:
        async with self._session_factory() as session:
            return await session.get(KnowledgeDocumentModel, document_id)

    async def find_by_content_hash(
        self, content_hash: str, *, scope: str, tenant_id: int | None
    ) -> KnowledgeDocumentModel | None:
        """按内容指纹查同 scope/租户下是否已有同一份文件（去重用）。已软删的不算。"""
        conditions = [
            KnowledgeDocumentModel.content_hash == content_hash,
            KnowledgeDocumentModel.scope == scope,
            KnowledgeDocumentModel.status != "deleted",
        ]
        conditions.append(
            KnowledgeDocumentModel.tenant_id.is_(None)
            if tenant_id is None
            else KnowledgeDocumentModel.tenant_id == tenant_id
        )
        stmt = select(KnowledgeDocumentModel).where(*conditions).order_by(KnowledgeDocumentModel.id)
        async with self._session_factory() as session:
            return (await session.scalars(stmt)).first()

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        tenant_id: int | None = None,
        scope: str | None = None,
        doc_type: str | None = None,
        index_status: str | None = None,
        status: str | None = None,
        include_global: bool = True,
        keyword: str | None = None,
    ) -> tuple[list[KnowledgeDocumentModel], int]:
        """分页列表。

        ``tenant_id`` 给定时表示"某个租户可见的文档"：默认把 ``scope='global'`` 一并带上
        （全局文档对所有租户可见）；``include_global=False`` 则只看该租户自己的。
        """
        conditions = []
        if tenant_id is not None:
            tenant_cond = KnowledgeDocumentModel.tenant_id == tenant_id
            if include_global:
                tenant_cond = or_(tenant_cond, KnowledgeDocumentModel.scope == "global")
            conditions.append(tenant_cond)
        if scope:
            conditions.append(KnowledgeDocumentModel.scope == scope)
        if doc_type:
            conditions.append(KnowledgeDocumentModel.doc_type == doc_type)
        if index_status:
            conditions.append(KnowledgeDocumentModel.index_status == index_status)
        if status:
            conditions.append(KnowledgeDocumentModel.status == status)
        if keyword:
            like = f"%{keyword.strip()}%"
            conditions.append(
                or_(
                    KnowledgeDocumentModel.filename.ilike(like),
                    KnowledgeDocumentModel.doc_name.ilike(like),
                )
            )
        count_stmt = select(func.count()).select_from(KnowledgeDocumentModel).where(*conditions)
        page_stmt = (
            select(KnowledgeDocumentModel)
            .where(*conditions)
            .order_by(KnowledgeDocumentModel.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        async with self._session_factory() as session:
            total = int((await session.scalar(count_stmt)) or 0)
            rows = list((await session.scalars(page_stmt)).all())
        return rows, total

    async def update(self, document_id: int, fields: dict) -> KnowledgeDocumentModel | None:
        """局部更新（改 index_status / status 等）；查不到返回 None。"""
        async with self._session_factory() as session:
            row = await session.get(KnowledgeDocumentModel, document_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
        return row

    async def soft_delete(self, document_id: int) -> KnowledgeDocumentModel | None:
        """软删：置 status='deleted' + deleted_at（向量清理由调用方负责）。"""
        return await self.update(
            document_id, {"status": "deleted", "deleted_at": _utcnow()}
        )


class DocumentIndexTaskRepository:
    """document_index_tasks 表。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_index_task(
        self,
        *,
        tenant_id: int | None,
        document_id: int,
        status: str = "QUEUED",
        retry_of_task_id: int | None = None,
    ) -> DocumentIndexTaskModel:
        row = DocumentIndexTaskModel(
            tenant_id=tenant_id,
            document_id=document_id,
            status=status,
            retry_of_task_id=retry_of_task_id,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
        return row

    async def find_by_id(self, task_id: int) -> DocumentIndexTaskModel | None:
        async with self._session_factory() as session:
            return await session.get(DocumentIndexTaskModel, task_id)

    async def find_latest_for_document(self, document_id: int) -> DocumentIndexTaskModel | None:
        stmt = (
            select(DocumentIndexTaskModel)
            .where(DocumentIndexTaskModel.document_id == document_id)
            .order_by(DocumentIndexTaskModel.id.desc())
            .limit(1)
        )
        async with self._session_factory() as session:
            return (await session.scalars(stmt)).first()

    async def update(self, task_id: int, fields: dict) -> DocumentIndexTaskModel | None:
        async with self._session_factory() as session:
            row = await session.get(DocumentIndexTaskModel, task_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
        return row

    async def set_background_job(
        self, task_id: int, background_job_id: int
    ) -> DocumentIndexTaskModel | None:
        """回填关联的后台任务 id（索引任务 → job 的追溯链靠它）。"""
        return await self.update(task_id, {"background_job_id": background_job_id})

    async def mark_running(self, task_id: int) -> DocumentIndexTaskModel | None:
        return await self.update(task_id, {"status": "RUNNING", "started_at": _utcnow()})

    async def mark_succeeded(self, task_id: int) -> DocumentIndexTaskModel | None:
        return await self.update(task_id, {"status": "SUCCEEDED", "completed_at": _utcnow()})

    async def mark_failed(
        self, task_id: int, reason: str
    ) -> DocumentIndexTaskModel | None:
        return await self.update(
            task_id,
            {"status": "FAILED", "completed_at": _utcnow(), "failure_reason": reason[:2000]},
        )

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        document_id: int | None = None,
        tenant_id: int | None = None,
        status: str | None = None,
    ) -> tuple[list[DocumentIndexTaskModel], int]:
        conditions = []
        if document_id is not None:
            conditions.append(DocumentIndexTaskModel.document_id == document_id)
        if tenant_id is not None:
            conditions.append(DocumentIndexTaskModel.tenant_id == tenant_id)
        if status:
            conditions.append(DocumentIndexTaskModel.status == status)
        count_stmt = select(func.count()).select_from(DocumentIndexTaskModel).where(*conditions)
        page_stmt = (
            select(DocumentIndexTaskModel)
            .where(*conditions)
            .order_by(DocumentIndexTaskModel.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        async with self._session_factory() as session:
            total = int((await session.scalar(count_stmt)) or 0)
            rows = list((await session.scalars(page_stmt)).all())
        return rows, total
