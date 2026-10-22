from __future__ import annotations

from collections.abc import Callable, Sequence

from langchain_openai import OpenAIEmbeddings

from app.backend.src.settings import LlmSettings

# 单次请求最多带多少条文本：DashScope 对单次输入条数限制比 OpenAI 严得多
DEFAULT_BATCH_SIZE = 10

EmbeddingFactory = Callable[[LlmSettings], OpenAIEmbeddings]


class EmbeddingError(RuntimeError):
    """嵌入调用失败（配置缺失 / 网络异常 / 返回不符合预期）。"""


def _create_openai_embedding_model(
    settings: LlmSettings, *, batch_size: int
) -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        chunk_size=batch_size,  # 底层按这个大小自动分批
        max_retries=settings.max_retries,
        timeout=settings.timeout_seconds,
        # 关键：别让 LangChain 用 tiktoken 去算上下文长度——那是 OpenAI 的分词器，
        # 对国内端点不适用（会报错或算错），src1 同样关掉了它。
        check_embedding_ctx_length=False,
    )


class EmbeddingModelProvider:
    def __init__(
        self,
        settings: LlmSettings,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        embedding_factory: EmbeddingFactory | None = None,
    ) -> None:
        self._settings = settings
        self._batch_size = max(1, batch_size)
        self._factory = embedding_factory  # 测试时可注入假的
        self._model: OpenAIEmbeddings | None = None

    @property
    def configured(self) -> bool:
        return self._settings.configured

    @property
    def built(self) -> bool:
        """是否已经建过底层客户端（只读状态，不触发构建）。"""
        return self._model is not None

    # ---------- 懒构建 ----------

    def _ensure_model(self) -> OpenAIEmbeddings:
        if self._model is not None:
            return self._model
        if not self._settings.configured:
            raise EmbeddingError(
                "未配置嵌入模型：请在 config/user.project.json 的 llm 段填写 apiKey 与 embeddingModel"
            )
        factory = self._factory or (
            lambda s: _create_openai_embedding_model(s, batch_size=self._batch_size)
        )
        self._model = factory(self._settings)
        return self._model

    # ---------- 对外 ----------

    def create_embeddings(self) -> OpenAIEmbeddings:
        """把底层 ``OpenAIEmbeddings`` 实例交出去。

        与 ``ChatModelProvider.create_chat_model`` 同理：LangGraph 的 ``BaseStore`` 配 ``index``
        时要的是**一个 Embeddings 对象**，不是"给我一段文本还我一个向量"的方法。同样是懒构建。
        """
        return self._ensure_model()

    async def aembed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """批量嵌入；返回与输入**一一对应**的向量。"""
        if not texts:
            return []
        model = self._ensure_model()
        try:
            vectors = await model.aembed_documents(list(texts))
        except EmbeddingError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 网络/鉴权/配额都要变成明确错误
            raise EmbeddingError(f"嵌入调用失败：{exc}") from exc

        if len(vectors) != len(texts):
            raise EmbeddingError(f"嵌入数量不匹配：请求 {len(texts)} 条，返回 {len(vectors)} 条")
        expected = self._settings.embedding_dimensions
        for vector in vectors:
            if len(vector) != expected:
                raise EmbeddingError(f"向量维度不符：期望 {expected}，实际 {len(vector)}")
        return vectors

    async def aembed_query(self, text: str) -> list[float]:
        """单条查询文本的向量（在线检索用）。"""
        return (await self.aembed_documents([text]))[0]
