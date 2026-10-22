from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from app.backend.src.services.retrieval_service import DEFAULT_TOP_K, RetrievalService

KNOWLEDGE_RETRIEVAL_TOOL_NAME = "knowledge_retrieval"

_NO_HIT_MESSAGE = "没有检索到相关片段。可以换个说法，或确认该文档是否已上传并完成索引。"


def build_knowledge_retrieval_tool(
    service: RetrievalService,
    *,
    tenant_id: int,
    include_global: bool = True,
    top_k: int = DEFAULT_TOP_K,
) -> Any:
    """生成一个绑定到具体租户的检索工具（每个会话/任务按需生成，别做成全局单例）。"""

    @tool(KNOWLEDGE_RETRIEVAL_TOOL_NAME)
    async def knowledge_retrieval(query: str) -> str:
        """检索设备手册、SOP 与历史维修案例。

        输入：一句自然语言问题（例如"主轴过载 E-1024 应该怎么处理"）。
        输出：最相关的若干原文片段，每条都带文档与段落位置，可直接作为回答依据。
        """
        result = await service.retrieve(
            query, tenant_id=tenant_id, include_global=include_global, top_k=top_k
        )
        if not result.hits:
            return _NO_HIT_MESSAGE
        blocks = [
            f"[{index}] 文档 {hit.document_id} 第 {hit.chunk_index} 段\n{hit.excerpt}"
            for index, hit in enumerate(result.hits, start=1)
        ]
        return "\n\n".join(blocks)

    return knowledge_retrieval
