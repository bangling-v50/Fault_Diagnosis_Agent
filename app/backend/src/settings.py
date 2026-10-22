from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.backend.src.vector_store.config import VectorStoreSettings

# 本文件位于 apps/backend/src/，仓库根是其第 3 级父目录
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_DIR = _REPO_ROOT / "config"
DEFAULT_PROJECT_CONFIG = _CONFIG_DIR / "project.json"
DEFAULT_USER_CONFIG = _CONFIG_DIR / "user.project.json"


@dataclass(frozen=True)
class LlmSettings:
    """模型服务设置（Qwen / DashScope 的 OpenAI 兼容端点）。"""

    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""
    embedding_model: str = ""
    embedding_dimensions: int = 1024
    chat_model: str | None = None
    rerank_model: str | None = None
    # rerank 不是 OpenAI 兼容接口，走 DashScope 的原生端点，所以要单独一个 url
    rerank_url: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    )
    timeout_seconds: int = 60
    max_retries: int = 2
    # 生成诊断报告的采样温度：诊断要的是稳定复现的结论，不是文采，所以压得很低
    temperature: float = 0.2
    # 上下文窗口（token）。聊天要拿它算"还剩多少余量、要不要压缩"。
    # 配置里写在 llm.modelCapabilities.<模型名>.contextWindowTokens
    context_window_tokens: int = 128000

    @property
    def configured(self) -> bool:
        """是否具备调用嵌入模型的最小配置（缺了也不该阻止应用启动，只是用不了 RAG）。"""
        return bool(self.api_key and self.embedding_model)

    @property
    def chat_configured(self) -> bool:
        """是否具备调用**聊天**模型的最小配置。

        与 ``configured`` 分开：两者用的是同一个 apiKey，但模型名各填各的，
        只配了 embedding 就去调聊天模型，应该在这里就被挡住。
        """
        return bool(self.api_key and self.chat_model)

@dataclass(frozen=True)
class Settings:
    database_url: str
    pool_size: int = 10
    max_overflow: int = 20
    pool_recycle_seconds: int = 1800
    session_ttl_seconds: int = 7 * 24 * 3600  # 登录态默认 7 天
    # 后台任务（durable job runtime）
    job_worker_concurrency: int = 2
    job_lease_seconds: int = 30
    job_poll_seconds: float = 0.5
    job_default_timeout_seconds: int = 900
    # 文档实体存储：S3 兼容协议——MinIO / 阿里云 OSS / AWS S3 是**同一套代码**，
    # 只差 endpoint 与凭证。上传的原文件与解析出的 ``.extracted.md`` 都放这里，
    # **服务器本地不留任何文档实体**。
    # 默认值指向本地 MinIO 容器（`infra/compose.yaml`），换云厂商时改这里即可。
    object_storage_endpoint: str = "http://127.0.0.1:9000"
    object_storage_access_key: str = "minioadmin"
    object_storage_secret_key: str = "minioadmin"
    object_storage_bucket: str = "fadia-documents"
    object_storage_region: str = "us-east-1"
    # 文档与索引
    doc_max_upload_bytes: int = 20 * 1024 * 1024  # 单文件 20MB
    doc_chunk_strategy: str = "fixed-character"  # 见 documents/splitter.py 支持的三种
    # ⚠️ 2026-09-21 由 500 调小到 200：**为了评测**。片段级召回率在"7 份文档 38 段"
    # 的粒度上没有区分度（top-5 命中文档即算成功，指标直接顶到 100%）。
    # 调到 200 后片段数约为原来的 2.5 倍（38 → 108 段），片段级指标才有分辨力。
    # 结果见 docs/rag-eval-results.md 第四节。
    #
    # ⚠️ 改这个值要**两步**，缺一不可：
    #   ① 重新索引全部文档（切分变了，向量要全部重算）；
    #   ② **重启后端**——`settings` 在 `create_app()` 时读一次就冻结进 Container 了。
    # 只做 ① 不做 ② 的话，重新索引会"成功"但片段数纹丝不动（踩过：任务全是 SUCCEEDED，
    # 切出来还是 390~478 字的老片段）。
    doc_chunk_size: int = 200
    doc_chunk_overlap: int = 50
    embedding_batch_size: int = 10
    # 向量召回的**相似度下限**：低于它的候选直接丢弃（否则"什么都能查出点东西"，
    # 无法表达"查不到"，模型就会拿不相关片段硬编）。
    # 取值来自实测（同一份语料）：相关片段余弦相似度 ≈0.93；不相关 0.42~0.54；
    # 完全不相关的查询对所有片段只有 0.20~0.25。0.5 落在"不相关"之上、"相关"之下。
    # 注意：**只过滤向量那一路**——BM25 命中的片段有词面依据，不该被相似度否掉。
    retrieval_min_similarity: float = 0.5
    chat_compact_after_turns: int = 30  # 未压缩区里 assistant 消息达到多少条就压缩
    chat_compact_percent: float = 70.0  # 或者上下文占用达到这个百分比就压缩
    chat_hard_context_percent: float = 95.0  # 超过就拒收新消息（避免直接撑爆窗口）
    chat_fallback_recent_messages: int = 40  # 兜底裁剪：无论如何至少保留最近这么多条
    llm: LlmSettings = field(default_factory=LlmSettings)
    vector_store: VectorStoreSettings = field(default_factory=VectorStoreSettings)


def _deep_merge(base: dict, override: dict) -> dict:
    """深合并：override 覆盖 base；两边都是 dict 时递归合并。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _context_window_tokens(raw_llm: dict) -> int:
    """从 ``llm.modelCapabilities.<chatModel>.contextWindowTokens`` 取上下文窗口大小。

    这个键是**按模型名分组的**（换模型了就得重新配），取不到就退回 128k 的保守默认值——
    宁可早压缩，也不要撞上真实窗口上限。
    """
    model = raw_llm.get("chatModel")
    capabilities = raw_llm.get("modelCapabilities") or {}
    if model and isinstance(capabilities.get(model), dict):
        value = capabilities[model].get("contextWindowTokens")
        if value:
            return int(value)
    return LlmSettings.context_window_tokens


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")) or {}


def load_settings(
    config_path: Path = DEFAULT_PROJECT_CONFIG,
    user_config_path: Path = DEFAULT_USER_CONFIG,
) -> Settings:
    data = _deep_merge(_load_json(config_path), _load_json(user_config_path))

    backend = data.get("backend") or {}
    database_url = os.environ.get("FADIA_DATABASE_URL") or backend.get("databaseUrl")
    if not database_url:
        raise RuntimeError(
            f"未配置 databaseUrl：检查 {config_path} 或设置环境变量 FADIA_DATABASE_URL"
        )

    raw_llm = data.get("llm") or {}
    llm = LlmSettings(
        base_url=str(raw_llm.get("baseUrl") or LlmSettings.base_url),
        api_key=str(raw_llm.get("apiKey") or ""),
        embedding_model=str(raw_llm.get("embeddingModel") or ""),
        embedding_dimensions=int(raw_llm.get("embeddingDimensions") or 1024),
        chat_model=raw_llm.get("chatModel") or None,
        rerank_model=raw_llm.get("rerankModel") or None,
        rerank_url=str(raw_llm.get("rerankUrl") or LlmSettings.rerank_url),
        timeout_seconds=int(raw_llm.get("timeoutSeconds") or 60),
        max_retries=int(raw_llm.get("maxRetries") or 2),
        temperature=float(raw_llm.get("temperature", 0.2)),
        context_window_tokens=_context_window_tokens(raw_llm),
    )

    raw_storage = data.get("objectStorage") or {}

    return Settings(
        database_url=database_url,
        object_storage_endpoint=str(
            raw_storage.get("endpoint") or Settings.object_storage_endpoint
        ),
        object_storage_access_key=str(
            raw_storage.get("accessKey") or Settings.object_storage_access_key
        ),
        object_storage_secret_key=str(
            raw_storage.get("secretKey") or Settings.object_storage_secret_key
        ),
        object_storage_bucket=str(
            raw_storage.get("bucket") or Settings.object_storage_bucket
        ),
        object_storage_region=str(
            raw_storage.get("region") or Settings.object_storage_region
        ),
        pool_size=int(backend.get("databasePoolSize", 10)),
        max_overflow=int(backend.get("databaseMaxOverflow", 20)),
        pool_recycle_seconds=int(backend.get("databasePoolRecycleSeconds", 1800)),
        llm=llm,
        vector_store=VectorStoreSettings.from_config(data.get("vectorStore")),
    )
