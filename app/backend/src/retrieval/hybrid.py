from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from rank_bm25 import BM25L

# 两路召回各自留给 RRF 的候选数。扩大到 40，降低相关片段在融合前被截断的概率。
BM25_CANDIDATE_LIMIT = 40
# RRF 的平滑常数：src1 与常见实践都用 60
RRF_K = 60

# ASCII/运维文本整体成一个 token；中文（含扩展区）整段成一块，稍后拆成单字+双字
_TOKEN_PATTERN = re.compile(
    r"[A-Za-z0-9]+(?:[._:/-][A-Za-z0-9]+)*|[㐀-䶿一-鿿]+"
)


@dataclass(frozen=True, slots=True)
class Bm25Rank:
    """一条关键词排名，``index`` 指回传入的 documents 序列。"""

    index: int
    score: float


@dataclass(frozen=True, slots=True)
class ReciprocalRank:
    """一条融合后的排名，附带它在两路里的原始位次（可追溯，便于排查）。"""

    key: str
    score: float
    vector_rank: int | None
    bm25_rank: int | None


def tokenize_hybrid_text(text: str) -> list[str]:
    """把中英混排文本切成 token（ASCII 整体、中文单字 + 双字）。"""
    tokens: list[str] = []
    for match in _TOKEN_PATTERN.finditer(text):
        value = match.group(0).lower()
        if _is_chinese_segment(value):
            characters = list(value)
            tokens.extend(characters)
            tokens.extend(
                "".join(characters[index : index + 2])
                for index in range(len(characters) - 1)
            )
        else:
            tokens.append(value)
    return tokens


def rank_bm25_documents(
    *,
    query: str,
    documents: Sequence[str],
    limit: int = BM25_CANDIDATE_LIMIT,
) -> list[Bm25Rank]:
    """用 BM25L 给文档打分，返回前 ``limit`` 名。

    **两道过滤**（第二道是我们相对 src1 补的，见下）：

    1. 排除与查询**完全没有 token 重叠**的文档（src1 的做法）；
    2. **要求至少有一个"长度 > 1"的 token 重合**（双字词或 ASCII 整体）。

    第 2 道为什么必要：中文按"单字 + 双字"分词后，**单个常用汉字**（全/不/的/是…）
    在无关文档里随处可见。实测一条完全无关的查询（"完全不相关的量子纠缠话题"）
    就是因为与「**安**全**须知**」共用了一个「全」，被 BM25 打了 **0.916** 的高分
    ——小语料里 IDF 虚高，分数阈值也救不了。要求"双字词或 ASCII 词"重合，
    等于要求**词级**依据，而不是碰巧撞上一个字。

    副作用（可接受）：**单字查询**（如只查"轴"）不再走关键词召回——那种查询本来就靠向量召回，
    放进 BM25 只会带来噪音。
    """
    query_tokens = tokenize_hybrid_text(query)
    if not query_tokens or not documents or limit < 1:
        return []
    strong_query_tokens = {token for token in query_tokens if len(token) > 1}
    if not strong_query_tokens:
        return []  # 查询里没有可用词（例如只有一个汉字）→ 交给向量召回

    corpus_tokens = [tokenize_hybrid_text(document) for document in documents]
    scorer = BM25L(corpus_tokens)
    scores = scorer.get_scores(query_tokens)
    ranks = [
        Bm25Rank(index=index, score=float(scores[index]))
        for index, tokens in enumerate(corpus_tokens)
        if set(tokens) & strong_query_tokens
    ]
    ranks.sort(key=lambda item: (-item.score, item.index))
    return ranks[:limit]


def reciprocal_rank_fusion(
    *,
    vector_keys: Sequence[str],
    bm25_keys: Sequence[str],
    limit: int,
    k: int = RRF_K,
) -> list[ReciprocalRank]:
    """RRF 融合两路 ranked-key 列表：``score = Σ 1/(k + rank)``。

    只看**位次**不看分数——这是 RRF 的好处：两路的分值尺度完全不同（余弦距离 vs BM25 分），
    没法直接加权，而位次是可比的。
    """
    if k < 1:
        raise ValueError("RRF 的 k 必须大于 0")

    positions: dict[str, dict[str, int]] = {}
    for source, keys in (("vector", vector_keys), ("bm25", bm25_keys)):
        for rank, key in enumerate(dict.fromkeys(keys), start=1):  # 去重：同一路里重复只算首次位次
            positions.setdefault(key, {})[source] = rank

    fused = [
        ReciprocalRank(
            key=key,
            score=sum(1 / (k + rank) for rank in source_positions.values()),
            vector_rank=source_positions.get("vector"),
            bm25_rank=source_positions.get("bm25"),
        )
        for key, source_positions in positions.items()
    ]
    fused.sort(
        key=lambda item: (
            -item.score,
            # 同分时先看"两路里较好的那个位次"，再按键名兜底 —— 保证确定性
            min(rank for rank in (item.vector_rank, item.bm25_rank) if rank is not None),
            item.key,
        )
    )
    return fused[: max(limit, 0)]


def _is_chinese_segment(value: str) -> bool:
    return bool(value) and "㐀" <= value[0] <= "鿿"
