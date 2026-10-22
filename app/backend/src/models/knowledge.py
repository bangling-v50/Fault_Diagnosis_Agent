from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.backend.src.models.base import (
    Base,
    BigIntPrimaryKey,
    CreatedAt,
    TimestampMixin,
)

# 与 embedding 模型的维度必须一致（见 config 的 llm.embeddingDimensions）。
# 写错维度时数据库会直接报错（vector(1024) 只接受 1024 维），这是最后一道防线。
EMBEDDING_DIMENSION = 1024


class KnowledgeDocumentModel(TimestampMixin, Base):
    """PostgreSQL metadata for a document whose chunks live in Milvus."""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint("scope IN ('global', 'tenant')", name="scope_allowed"),
        CheckConstraint(
            "(scope = 'global' AND tenant_id IS NULL) OR "
            "(scope = 'tenant' AND tenant_id IS NOT NULL)",
            name="scope_matches_tenant",
        ),
        CheckConstraint(
            # ⚠️ 加新值时**这里和数据库要一起改**（对着库跑一次 ALTER，见 docs 的接线清单）。
            # multimodal_* 两个值是多模态管线用的：表格类文件 / 图片与扫描件。
            "doc_type IN ('manual', 'sop', 'diagnostic_case', 'faq', "
            "'multimodal_table', 'multimodal_image')",
            name="doc_type_allowed",
        ),
        CheckConstraint("status IN ('active', 'deleted')", name="status_allowed"),
        CheckConstraint(
            "index_status IN ('pending', 'indexing', 'indexed', 'failed')",
            name="index_status_allowed",
        ),
        CheckConstraint(
            "source_type IN ('upload', 'diagnosis_case', 'system')",
            name="source_type_allowed",
        ),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="size_non_negative"),
        Index("ix_knowledge_documents_scope_tenant", "scope", "tenant_id"),
    )

    id: Mapped[BigIntPrimaryKey]
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    doc_name: Mapped[str | None] = mapped_column(String(255))
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(160))
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(96), index=True)
    status: Mapped[str] = mapped_column(
        String(40), nullable=False, default="active", server_default="active"
    )
    index_status: Mapped[str] = mapped_column(
        String(40), nullable=False, default="pending", server_default="pending"
    )
    source_type: Mapped[str] = mapped_column(
        String(40), nullable=False, default="upload", server_default="upload"
    )
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentIndexTaskModel(TimestampMixin, Base):
    """A single indexing attempt for a knowledge document."""

    __tablename__ = "document_index_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="status_allowed",
        ),
        Index("ix_document_index_tasks_document_created", "document_id", "created_at"),
        Index("ix_document_index_tasks_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[BigIntPrimaryKey]
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    document_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    background_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("background_jobs.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        String(40), nullable=False, default="QUEUED", server_default="QUEUED"
    )
    retry_of_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_index_tasks.id", ondelete="SET NULL")
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeParentChunkModel(Base):
    """**父块**：一个二级标题（``##``）下的整块内容。

    父块**不进向量库**——它没有 ``embedding`` 列，因为它的职责不是"被召回"，
    而是"被子块找到之后，把完整上下文交出去"。子块靠 ``knowledge_chunks.parent_id``
    指回来。

    跟子块分两张表而不是同表加个 flag，是为了让"能不能被检索到"这件事**由表决定**：
    检索永远只查 ``knowledge_chunks``，物理上不可能把父块误召回；反过来，父块的
    写入路径也永远不会去生成 embedding。同表加 flag 的话，每处检索都得记得带条件，
    漏一处就是"父块自己被召回、返回的还是父块"，而且看不出哪里错了。

    ``heading_path`` 是这一节的标题（如 ``## 1. 告警码总览``），切片时要存下来——
    子块的标题路径就是由它 + 三级标题拼出来的。
    """

    __tablename__ = "knowledge_parent_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "parent_index", name="uq_parent_chunks_document_index"),
        CheckConstraint("scope IN ('global', 'tenant')", name="scope_allowed"),
        CheckConstraint("parent_index >= 0", name="parent_index_non_negative"),
        Index("ix_knowledge_parent_chunks_tenant_scope", "tenant_id", "scope"),
    )

    id: Mapped[BigIntPrimaryKey]
    document_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_index: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[CreatedAt]


class KnowledgeChunkModel(Base):
    """文档切分后的片段：正文 + 向量 + 归属，全部存在 PostgreSQL 里（pgvector）。

    **这是子块**（能被检索的那一层）。带父块模式索引时，``parent_id`` 指回
    :class:`KnowledgeParentChunkModel`；老数据（父子块之前切的）``parent_id`` 为 NULL。

    与之前的 Milvus 方案相比，三处细节不一样，都是"能在同一个库里做"带来的简化：

    - **``tenant_id`` 可以直接是 NULL** 表示 global——Milvus 的标量字段不便存 NULL，
      那时只能拿 ``0`` 当哨兵值；现在语义干净了；
    - **唯一性交给数据库**：``UNIQUE (document_id, chunk_index)`` 保证"同一文档同一段只可能有一条"，
      写入用 ``ON CONFLICT DO UPDATE`` 覆盖（Milvus 里主键是我们自己拼的 ``"{doc}:{idx}"`` 字符串）；
    - **向量是原生类型** ``vector(1024)``，配 HNSW + cosine 索引，查询用 ``<=>``（余弦距离）。

    切成片段就不可变了，所以只带 ``created_at``，不带 updated_at。
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_knowledge_chunks_document_chunk"
        ),
        CheckConstraint("scope IN ('global', 'tenant')", name="scope_allowed"),
        CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        # 子块类型。**允许 NULL**——父子块之前切出来的老数据没有这个信息。
        CheckConstraint(
            "element_type IS NULL OR element_type IN ('paragraph', 'table_head', 'table_row')",
            name="element_type_allowed",
        ),
        # 租户 + scope 是检索时最常用的过滤组合
        Index("ix_knowledge_chunks_tenant_scope", "tenant_id", "scope"),
        # 向量索引：HNSW + cosine。<-> 与 <=> 各有对应的 ops，必须与查询用的操作符匹配。
        # 参数（m / ef_construction）写在这里而不是 config——索引属于 schema，
        # 由建表语句一起创建；config 里的 indexParams 对 pgvector 已不再生效。
        Index(
            "ix_knowledge_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 200},
        ),
    )

    id: Mapped[BigIntPrimaryKey]
    document_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 父块（同一篇文档内的整节）。**可空**：父子块之前索引的老数据没有父块；
    # 新流程里每个子块都有。ON DELETE CASCADE——父块是这段内容的归属，父块没了子块也没意义。
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_parent_chunks.id", ondelete="CASCADE"), index=True
    )
    # 子块是"一段正文"还是"表格的一行"（表格说明段算 table_head）
    element_type: Mapped[str | None] = mapped_column(String(24))
    # 标题路径（"## 1. 告警码总览 / ### 2.2 常见告警"）。正文里也带了一份（模型要读），
    # 这一列是给"按章节过滤/展示"用的，不用再去正文里扒。
    heading_path: Mapped[str | None] = mapped_column(String(512))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION), nullable=False)
    created_at: Mapped[CreatedAt]
