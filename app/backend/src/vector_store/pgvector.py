from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backend.src.models.knowledge import (
    KnowledgeChunkModel,
    KnowledgeParentChunkModel,
)
from app.backend.src.vector_store.config import VectorStoreSettings

logger = logging.getLogger(__name__)

# asyncpg 的单条语句最多接收 32767 个参数。每个子块当前有 9 个字段，
# 大文档不能把全部子块塞进同一条 INSERT；500 条一批也给未来新增字段留足余量。
CHUNK_INSERT_BATCH_SIZE = 500


def build_tenant_condition(tenant_id: int, *, include_global: bool = True) -> ColumnElement[bool]:
    """租户隔离的 SQL 条件：本租户的片段，可选带上 global 文档。

    这是**隔离规则的唯一定义处**。Milvus 版本里它是 ``build_tenant_filter``（返回表达式字符串），
    pgvector 版本里就是普通的 SQLAlchemy 条件，直接拼进 ``WHERE``。
    """
    condition = KnowledgeChunkModel.tenant_id == tenant_id
    if include_global:
        return or_(condition, KnowledgeChunkModel.scope == "global")
    return condition


@dataclass(frozen=True, slots=True)
class VectorHit:
    """向量召回的一条结果：片段 + 余弦**距离**（越小越相似）。"""

    chunk: KnowledgeChunkModel
    distance: float


@dataclass(frozen=True, slots=True)
class ParentRecord:
    """一条待写入的**父块**（整节内容，不带向量）。"""

    document_id: int
    parent_index: int
    heading_path: str
    content: str
    scope: str = "tenant"
    tenant_id: int | None = None

    def to_row(self) -> dict:
        return {
            "document_id": self.document_id,
            "parent_index": self.parent_index,
            "heading_path": self.heading_path,
            "content": self.content,
            "scope": self.scope,
            "tenant_id": self.tenant_id,
        }


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """一条待写入的**子块**（带向量，参与召回）。"""

    document_id: int
    chunk_index: int
    content: str
    embedding: list[float]
    scope: str = "tenant"
    tenant_id: int | None = None  # None 表示 global 文档（PG 里可以直接存 NULL）
    # 父子块模式索引时指回所属父块；老数据（或没走父子块的类型）为 None
    parent_id: int | None = None
    element_type: str | None = None  # paragraph / table_head / table_row
    heading_path: str | None = None

    def to_row(self) -> dict:
        return {
            "document_id": self.document_id,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "embedding": self.embedding,
            "scope": self.scope,
            "tenant_id": self.tenant_id,
            "parent_id": self.parent_id,
            "element_type": self.element_type,
            "heading_path": self.heading_path,
        }


class PGVectorStore:
    """基于 PostgreSQL + pgvector 的片段存储。

    与 ``MilvusVectorStore`` 最大的不同：**它不持有连接**，而是复用应用已有的
    ``session_factory``（同一个连接池、同一套事务），所以 ``close()`` 是空操作。
    """

    backend = "pgvector"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: VectorStoreSettings | None = None,
    ) -> None:
        self._session_factory = session_factory
        # settings 现在只剩"信息性"用途（维度/检索参数）：
        # 索引参数写在模型里由建表语句创建，URI/集合名这些 Milvus 概念已经没有意义
        self._settings = settings

    @property
    def settings(self) -> VectorStoreSettings | None:
        return self._settings

    # ---------- 写入 / 删除 ----------

    async def save_parents(self, parents: Sequence[ParentRecord]) -> dict[int, int]:
        """批量写入父块，返回 ``{parent_index: 父块 id}``。

        **为什么要返回 id**：子块的 ``parent_id`` 得指向父块，而 id 是数据库生成的，
        插进去之前不知道。所以这里用 ``RETURNING`` 把 id 捞回来，上层再据此填子块。
        （``ON CONFLICT DO UPDATE`` 时 RETURNING 返回的是**被更新那一行**的 id，
        所以重复索引同一份文档也能拿到正确的 id。）

        父块**不生成向量**——它的职责是被子块找到之后把上下文交出去，不参与召回。
        """
        if not parents:
            return {}
        stmt = insert(KnowledgeParentChunkModel).values([p.to_row() for p in parents])
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                KnowledgeParentChunkModel.document_id,
                KnowledgeParentChunkModel.parent_index,
            ],
            set_={
                "heading_path": stmt.excluded.heading_path,
                "content": stmt.excluded.content,
                "scope": stmt.excluded.scope,
                "tenant_id": stmt.excluded.tenant_id,
            },
        ).returning(KnowledgeParentChunkModel.parent_index, KnowledgeParentChunkModel.id)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
            await session.commit()
        return {int(index): int(pid) for index, pid in rows}

    async def save_chunks(self, chunks: Sequence[ChunkRecord]) -> int:
        """批量写入片段：``INSERT ... ON CONFLICT (document_id, chunk_index) DO UPDATE``。

        大文档按固定大小拆成多条语句，避免超过 asyncpg 的 32767 参数上限；所有批次共用
        同一个事务，只在全部成功后提交，因此不会留下只写入一部分子块的中间状态。
        """
        if not chunks:
            return 0
        async with self._session_factory() as session:
            for start in range(0, len(chunks), CHUNK_INSERT_BATCH_SIZE):
                rows = [
                    chunk.to_row()
                    for chunk in chunks[start : start + CHUNK_INSERT_BATCH_SIZE]
                ]
                stmt = insert(KnowledgeChunkModel).values(rows)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        KnowledgeChunkModel.document_id,
                        KnowledgeChunkModel.chunk_index,
                    ],
                    set_={
                        "content": stmt.excluded.content,
                        "embedding": stmt.excluded.embedding,
                        "scope": stmt.excluded.scope,
                        "tenant_id": stmt.excluded.tenant_id,
                        "parent_id": stmt.excluded.parent_id,
                        "element_type": stmt.excluded.element_type,
                        "heading_path": stmt.excluded.heading_path,
                    },
                )
                await session.execute(stmt)
            await session.commit()
        return len(chunks)

    async def delete_document_content(self, document_id: int) -> int:
        """删除某份文档的全部**内容**（父块 + 子块），返回删掉的总行数。

        两者是同一次切分的产物，**必须一起走**：只留父块，它永远不会被检索到却占着
        空间；只留子块，检索命中后取不到上下文。

        **两句都要**，而且顺序有讲究：

        1. 先删父块——``knowledge_chunks.parent_id`` 是 ``ON DELETE CASCADE``，
           父块一走，挂在它下面的子块由数据库自己带走；
        2. 再补一句删子块——**历史数据的 ``parent_id`` 是 NULL**（父子块之前索引的），
           不受级联影响，不补这一句就会留下 267 条永远清不掉的孤儿。

        ⚠️ 返回值是**删除前先数出来的**，不是两句 DELETE 的 ``rowcount`` 之和——
        级联删掉的行**不计入任何一句的 rowcount**（实测：2 父块 + 5 子块，两句加起来只有 3）。
        要如实报告"清了多少"，只能先数。

        （软删文档时也要调它：文件可以保留，向量必须清掉，否则已删内容还能被检索到。）
        """
        async with self._session_factory() as session:
            total = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(KnowledgeChunkModel)
                        .where(KnowledgeChunkModel.document_id == document_id)
                    )
                ).scalar()
                or 0
            ) + int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(KnowledgeParentChunkModel)
                        .where(KnowledgeParentChunkModel.document_id == document_id)
                    )
                ).scalar()
                or 0
            )

            await session.execute(
                delete(KnowledgeParentChunkModel).where(
                    KnowledgeParentChunkModel.document_id == document_id
                )
            )
            await session.execute(
                delete(KnowledgeChunkModel).where(KnowledgeChunkModel.document_id == document_id)
            )
            await session.commit()
            return total

    # ---------- 查询（统计与排查；相似度检索在下一分支） ----------

    @staticmethod
    def _conditions(
        *,
        document_id: int | None,
        tenant_id: int | None,
        include_global: bool,
    ) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = []
        if document_id is not None:
            conditions.append(KnowledgeChunkModel.document_id == document_id)
        if tenant_id is not None:
            conditions.append(build_tenant_condition(tenant_id, include_global=include_global))
        return conditions

    # 向量检索
    async def search_chunks(
        self,
        query_vector: Sequence[float],
        *,
        tenant_id: int | None = None,
        include_global: bool = True,
        limit: int = 20,
        document_ids: Sequence[int] | None = None,
    ) -> list[VectorHit]:
        """向量召回：按**余弦距离**取最近的片段（`<=>` 越小越相似）。

        两条过滤都下推到 SQL，所以取回来的一定在调用者可见的范围内
        ——不依赖上层"记得再加一次过滤"（这是 pgvector 相对 Milvus 最实在的好处之一）。
        """
        conditions = self._conditions(
            document_id=None, tenant_id=tenant_id, include_global=include_global
        )
        if document_ids:
            conditions.append(KnowledgeChunkModel.document_id.in_(list(document_ids)))
        distance = KnowledgeChunkModel.embedding.cosine_distance(list(query_vector)).label(
            "distance"
        )
        stmt = (
            select(KnowledgeChunkModel, distance)
            .where(*conditions)
            .order_by(distance)
            .limit(limit)
        )
        async with self._session_factory() as session:
            await self._apply_search_params(session)
            rows = (await session.execute(stmt)).all()
        return [VectorHit(chunk=row[0], distance=float(row[1])) for row in rows]

    async def _apply_search_params(self, session: AsyncSession) -> None:
        """把配置里的 ``hnsw.ef_search`` 应用到本次查询。

        索引参数（m / ef_construction）写在模型里、建表时生效；``ef_search`` 是**查询时**的
        精度/速度旋钮（越大越准越慢）。用 ``SET LOCAL`` 是因为它只作用于**当前事务**，
        不会泄漏给别的请求。
        """
        ef = None
        if self._settings is not None:
            ef = (self._settings.search_params or {}).get("ef")
        if ef:
            # int() 兜底：不把配置里的值直接拼进 SQL
            await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef)}"))

    async def count_chunks(
        self,
        *,
        document_id: int | None = None,
        tenant_id: int | None = None,
        include_global: bool = True,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(KnowledgeChunkModel)
            .where(*self._conditions(document_id=document_id, tenant_id=tenant_id, include_global=include_global))
        )
        async with self._session_factory() as session:
            return int((await session.scalar(stmt)) or 0)

    async def list_chunks(
        self,
        *,
        document_id: int | None = None,
        tenant_id: int | None = None,
        include_global: bool = True,
        document_ids: Sequence[int] | None = None,
        limit: int | None = 200,
    ) -> list[KnowledgeChunkModel]:
        """按条件列出片段（**按文档 + 段落序号排序**，方便拼回原文顺序）。

        ``limit=None`` 表示**不限制**（全量拉）——BM25 关键词召回需要整个语料，
        src1 那边对应的是 ``list_chunks(limit=-1)``。默认 200 只是防手滑拉爆内存。
        """
        conditions = self._conditions(
            document_id=document_id, tenant_id=tenant_id, include_global=include_global
        )
        if document_ids:
            conditions.append(KnowledgeChunkModel.document_id.in_(list(document_ids)))
        stmt = (
            select(KnowledgeChunkModel)
            .where(*conditions)
            .order_by(KnowledgeChunkModel.document_id, KnowledgeChunkModel.chunk_index)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._session_factory() as session:
            return list((await session.scalars(stmt)).all())

    async def close(self) -> None:
        """空操作：连接池归 ``Container`` 管，这里不持有连接。

        保留这个方法是为了让上层的 ``aclose`` 不用为"换实现"加分支。
        """
        return None
