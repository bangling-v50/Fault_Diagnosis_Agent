from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.backend.src.llm_provider.embedding import EmbeddingModelProvider
from app.backend.src.llm_provider.rerank import RerankError, RerankModelProvider
from app.backend.src.models.knowledge import KnowledgeChunkModel
from app.backend.src.retrieval.hybrid import (
    BM25_CANDIDATE_LIMIT,
    RRF_K,
    rank_bm25_documents,
    reciprocal_rank_fusion,
)
from app.backend.src.vector_store.pgvector import PGVectorStore

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 5
MAX_TOP_K = 5
VECTOR_CANDIDATE_LIMIT = 40
RERANK_CANDIDATE_LIMIT = 40
MAX_EXCERPT_CHARS = 480


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """一条检索结果。"""

    chunk_id: int
    document_id: int
    chunk_index: int
    content: str
    excerpt: str
    vector_rank: int | None  # 在向量召回里的位次（1 起）
    bm25_rank: int | None  # 在关键词召回里的位次
    rrf_score: float
    rerank_score: float | None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    query: str
    hits: list[RetrievalHit] = field(default_factory=list)
    # 观测信息：两路各召回多少候选、精排有没有生效
    vector_candidates: int = 0
    bm25_candidates: int = 0
    rerank_applied: bool = False


class RetrievalService:
    def __init__(
        self,
        *,
        vector_store: PGVectorStore,
        embedding_model: EmbeddingModelProvider,
        rerank_model: RerankModelProvider,
        min_similarity: float = 0.5,
    ) -> None:
        self._vector_store = vector_store
        self._embedding_model = embedding_model
        self._rerank_model = rerank_model
        self._min_similarity = min_similarity

    # 检索
    async def retrieve(
        self,
        query: str,
        *,
        tenant_id: int,
        top_k: int = DEFAULT_TOP_K,
        include_global: bool = True,
        document_ids: Sequence[int] | None = None,
    ) -> RetrievalResult:
        """检索该租户可见的片段（含 global 文档），返回精排后的前 ``top_k`` 条。"""
        normalized = query.strip()
        if not normalized:
            return RetrievalResult(query=query)
        top_k = max(1, min(top_k, MAX_TOP_K))

        # 1) 向量召回（顺便丢掉"相似度不够"的：余弦距离 = 1 - 相似度）
        query_vector = await self._embedding_model.aembed_query(normalized)
        raw_vector_hits = await self._vector_store.search_chunks(
            query_vector,
            tenant_id=tenant_id,
            include_global=include_global,
            document_ids=document_ids,
            limit=VECTOR_CANDIDATE_LIMIT,
        )
        # 过滤过低相似度结果
        vector_hits = [
            hit for hit in raw_vector_hits if 1.0 - hit.distance >= self._min_similarity
        ]

        # 2) 关键词召回：语料 = 同范围内的**全部**片段（照 src1，不设上限）
        corpus = await self._vector_store.list_chunks(
            tenant_id=tenant_id,
            include_global=include_global,
            document_ids=document_ids,
            limit=None,
        )
        bm25_ranks = rank_bm25_documents(
            query=normalized,
            documents=[chunk.content for chunk in corpus],
            limit=BM25_CANDIDATE_LIMIT,
        )

        # 3) RRF 融合：以 chunk id 为 key（同一片段必须能在两路里对上）
        vector_keys = [str(hit.chunk.id) for hit in vector_hits]
        bm25_keys = [str(corpus[rank.index].id) for rank in bm25_ranks]
        fused = reciprocal_rank_fusion(
            vector_keys=vector_keys,
            bm25_keys=bm25_keys,
            limit=RERANK_CANDIDATE_LIMIT,
            k=RRF_K,
        )
        if not fused:
            # 两路都没有可用候选 —— 这才是"真的查不到"
            return RetrievalResult(
                query=normalized,
                vector_candidates=len(vector_hits),
                bm25_candidates=len(bm25_ranks),
            )

        # 候选池：BM25 召回的片段可能不在向量候选里，两边都要收
        pool: dict[str, KnowledgeChunkModel] = {str(hit.chunk.id): hit.chunk for hit in vector_hits}
        pool.update({str(chunk.id): chunk for chunk in corpus})
        candidates = [pool[item.key] for item in fused if item.key in pool]

        # 4) rerank 精排（失败只降级，不中断检索）
        rerank_scores: dict[str, float] = {}
        rerank_applied = False
        if candidates:
            try:
                reranked = await self._rerank_model.arerank(
                    query=normalized,
                    documents=[chunk.content for chunk in candidates],
                    top_n=min(top_k, len(candidates)),
                )
            except RerankError as exc:
                logger.warning("精排失败，降级为 RRF 顺序：%s", exc)
            else:
                # 如果rerank成功了，则更新候选列表，并标记使用了rerank
                rerank_scores = {
                    str(candidates[item.index].id): item.relevance_score for item in reranked
                }
                candidates = [candidates[item.index] for item in reranked]
                rerank_applied = True

        # 5) 组装结果（带原始位次，便于追溯）
        positions = {item.key: item for item in fused}
        hits = []
        for chunk in candidates[:top_k]:
            key = str(chunk.id)
            position = positions.get(key)
            hits.append(
                RetrievalHit(
                    chunk_id=chunk.id,
                    document_id=chunk.document_id,
                    chunk_index=chunk.chunk_index,
                    content=chunk.content,
                    excerpt=_excerpt(chunk.content),
                    vector_rank=position.vector_rank if position else None,
                    bm25_rank=position.bm25_rank if position else None,
                    rrf_score=position.score if position else 0.0,
                    rerank_score=rerank_scores.get(key),
                )
            )
        return RetrievalResult(
            query=normalized,
            hits=hits,
            vector_candidates=len(vector_hits),
            bm25_candidates=len(bm25_ranks),
            rerank_applied=rerank_applied,
        )


def _excerpt(content: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    """给引用用的短片段（模型/前端展示用，别把整段都塞进去）。"""
    text = content.strip()
    return text if len(text) <= limit else f"{text[:limit]}…"
