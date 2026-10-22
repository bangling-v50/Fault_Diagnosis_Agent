from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx

from app.backend.src.settings import LlmSettings

RETRYABLE_STATUS = (429, 500, 502, 503, 504)


@dataclass(frozen=True, slots=True)
class RerankResult:
    """一条精排结果：``index`` 指向输入 documents 的下标，``relevance_score`` 越大越相关。"""

    index: int
    relevance_score: float


class RerankError(RuntimeError):
    """精排失败（**不含**服务端响应细节，可安全回给调用方）。"""


class RerankModelProvider:
    def __init__(
        self,
        settings: LlmSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client  # 测试时可注入
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self._settings.api_key and self._settings.rerank_model)

    async def arerank(
        self,
        *,
        query: str,
        documents: Sequence[str],
        top_n: int,
    ) -> list[RerankResult]:
        """对 documents 重新排序，返回按相关度倒序的前 ``top_n`` 条（下标指向入参位置）。"""
        normalized_query = query.strip()
        if not normalized_query or not documents:
            return []
        if top_n < 1:
            raise RerankError("精排参数不合法：top_n 必须大于 0")
        if not self.configured:
            raise RerankError(
                "未配置精排模型：请在 config 的 llm 段填写 apiKey 与 rerankModel"
            )
        top_n = min(top_n, len(documents))

        payload = {
            "model": self._settings.rerank_model,
            "input": {
                "query": {"text": normalized_query},
                "documents": [{"text": document} for document in documents],
            },
            "parameters": {"return_documents": False, "top_n": top_n},
        }
        response = await self._post_with_retry(payload)
        return _parse_results(response, document_count=len(documents), top_n=top_n)

    # ---------- 内部 ----------

    async def _post_with_retry(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        headers = {"Authorization": f"Bearer {self._settings.api_key}"}
        last_error: Exception | None = None
        for attempt in range(self._settings.max_retries + 1):
            try:
                response = await self._post(payload, headers)
                if response.status_code in RETRYABLE_STATUS and attempt < self._settings.max_retries:
                    await asyncio.sleep(0.25 * (2**attempt))
                    continue
                response.raise_for_status()
                parsed = response.json()
                if not isinstance(parsed, Mapping):
                    raise RerankError("精排服务返回了非预期的响应结构")
                return parsed
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt < self._settings.max_retries:
                    await asyncio.sleep(0.25 * (2**attempt))
                    continue
                break
            except httpx.HTTPStatusError as exc:
                # 4xx（鉴权/参数错）重试也没用，直接失败；不带响应体
                raise RerankError(f"精排请求被拒绝（HTTP {exc.response.status_code}）") from exc
            except ValueError as exc:  # 响应不是 JSON
                raise RerankError("精排服务返回了无法解析的响应") from exc
        raise RerankError("精排服务暂时不可用") from last_error

    async def _post(
        self, payload: Mapping[str, object], headers: Mapping[str, str]
    ) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(
                self._settings.rerank_url,
                headers=dict(headers),
                json=dict(payload),
                timeout=self._settings.timeout_seconds,
            )
        # 懒建客户端：与 Milvus / embedding 一致，用到才建
        self._client = httpx.AsyncClient(timeout=self._settings.timeout_seconds)
        return await self._client.post(
            self._settings.rerank_url, headers=dict(headers), json=dict(payload)
        )

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


def _parse_results(
    payload: Mapping[str, object], *, document_count: int, top_n: int
) -> list[RerankResult]:
    """严格解析 ``output.results``；任何不合规都直接失败，不猜。"""
    output = payload.get("output")
    if not isinstance(output, Mapping):
        raise RerankError("精排服务返回了无效的响应结构")
    raw_results = output.get("results")
    if not isinstance(raw_results, list):
        raise RerankError("精排服务返回了无效的响应结构")

    results: list[RerankResult] = []
    seen: set[int] = set()
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            raise RerankError("精排服务返回了无效的响应结构")
        index = raw.get("index")
        score = raw.get("relevance_score")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < document_count
            or index in seen
            or not isinstance(score, int | float)
            or isinstance(score, bool)
        ):
            raise RerankError("精排服务返回了无效的响应结构")
        value = float(score)
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise RerankError("精排服务返回了无效的分数")
        seen.add(index)
        results.append(RerankResult(index=index, relevance_score=value))

    results.sort(key=lambda item: item.relevance_score, reverse=True)
    return results[:top_n]
