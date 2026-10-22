from __future__ import annotations

from collections.abc import Callable

from langchain_openai import ChatOpenAI

from app.backend.src.settings import LlmSettings

ChatFactory = Callable[[LlmSettings], ChatOpenAI]


class ChatError(RuntimeError):
    """聊天模型调用失败（配置缺失 / 网络异常 / 返回不符合预期）。"""


def _create_chat_openai_model(settings: LlmSettings) -> ChatOpenAI:
    return ChatOpenAI(
        api_key=settings.api_key,
        base_url=settings.base_url,
        model=settings.chat_model or "",
        temperature=settings.temperature,
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
    )


class ChatModelProvider:
    def __init__(
        self,
        settings: LlmSettings,
        *,
        chat_factory: ChatFactory | None = None,
    ) -> None:
        self._settings = settings
        self._factory = chat_factory  # 测试时可注入假的
        self._model: ChatOpenAI | None = None

    @property
    def configured(self) -> bool:
        return self._settings.chat_configured

    @property
    def built(self) -> bool:
        """是否已经建过底层客户端（只读状态，不触发构建）。"""
        return self._model is not None

    @property
    def model_name(self) -> str:
        """落进 ``diagnosis_reports.llm_model``，回答"这份报告是哪次、哪个模型出的"。"""
        return self._settings.chat_model or ""

    # ---------- 懒构建 ----------

    def _ensure_model(self) -> ChatOpenAI:
        if self._model is not None:
            return self._model
        if not self._settings.chat_configured:
            raise ChatError(
                "未配置聊天模型：请在 config/user.project.json 的 llm 段填写 apiKey 与 chatModel"
            )
        factory = self._factory or _create_chat_openai_model
        self._model = factory(self._settings)
        return self._model

    # ---------- 对外 ----------

    def create_chat_model(self) -> ChatOpenAI:
        """把底层 ``ChatOpenAI`` 实例交出去。

        ``ainvoke(prompt) -> str`` 那种"发一段字、拿一段字"的用法够诊断 workflow 用，
        但 **Agent 装配要的是模型对象本身**（``create_agent(model=...)`` 收 ``BaseChatModel``），
        所以这里必须开口子。同样是懒构建——调用它才会真的建客户端。
        """
        return self._ensure_model()

    async def ainvoke(self, prompt: str) -> str:
        """发一段提示词，拿回纯文本（``content`` 是 list 时拼成字符串）。"""
        model = self._ensure_model()
        try:
            message = await model.ainvoke(prompt)
        except ChatError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 网络/鉴权/配额都要变成明确错误
            raise ChatError(f"聊天模型调用失败：{exc}") from exc
        return message_text(message.content)

    # 没有 close/aclose：langchain-core 1.6 的 ChatOpenAI 并不暴露关闭方法（连接由它内部的
    # openai 客户端自管，进程退出即释放）。``EmbeddingModelProvider`` 同理——这里保持对称，
    # 不去调一个不存在的 API。真正需要显式关的是自己持有 httpx 的 ``RerankModelProvider``。


def message_text(content: object) -> str:
    """把消息的 ``content`` 统一成 str。

    list 的形态是 ``[{"type": "text", "text": "..."}, ...]``（多模态块），
    只取 text 块拼接；非文本块（图片等）直接跳过——诊断链路里不会用到它们。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)
