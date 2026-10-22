from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.backend.src.models.base import (
    Base,
    BigIntPrimaryKey,
    CreatedAt,
    JsonObject,
    TimestampMixin,
)


class ChatSessionModel(TimestampMixin, Base):
    """A tenant-scoped Chat Agent session linked to a LangGraph thread."""

    __tablename__ = "chat_sessions"
    __table_args__ = (
        CheckConstraint(
            "memory_mode IN ('every_30_turns', 'context_70_percent', 'manual')",
            name="memory_mode_allowed",
        ),
        Index("ix_chat_sessions_tenant_user_updated", "tenant_id", "user_id", "updated_at"),
    )

    id: Mapped[BigIntPrimaryKey]
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(240))
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    alert_id: Mapped[int | None] = mapped_column(ForeignKey("alerts.id", ondelete="SET NULL"))
    diagnosis_report_id: Mapped[int | None] = mapped_column(
        ForeignKey("diagnosis_reports.id", ondelete="SET NULL")
    )
    memory_summary: Mapped[str | None] = mapped_column(Text)
    compacted_message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 压缩策略：按轮数 / 按上下文占用百分比 / 只手动。取值见 CHECK
    memory_mode: Mapped[str] = mapped_column(
        String(40), nullable=False, default="every_30_turns", server_default="every_30_turns"
    )
    # 当前"喂给模型的那一份"估算占多少 token（不含被压缩掉的部分），用来判断要不要再压
    context_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_compacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 长期记忆的"已提炼"标记：没有它，"新会话第一句触发提炼上一个"会反复提炼同一个会话，
    # 记忆库里就会堆出一堆重复条目。NULL 表示还没提炼过
    memory_extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatMessageModel(Base):
    """A durable user, assistant, system, or tool message.

    ``tool`` 这一档是刻意留的：工具调用的结果要**落库并回放给模型**，否则用户追问
    "你刚才查到的那个工单里方案是什么"时，模型根本不知道自己查过。工具轮次在表里长这样：

        user → assistant(tool_calls 存在 metadata 里) → tool(结果) → assistant(最终回答)

    ``tool`` 行只给模型看；给前端的历史列表默认把它过滤掉（前端看的是"谁说了什么"，
    不是"模型看到了什么"）。
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'system', 'tool')", name="role_allowed"),
        Index("ix_chat_messages_session_created", "session_id", "created_at"),
    )

    id: Mapped[BigIntPrimaryKey]
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[JsonObject] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[CreatedAt]


class UserMemoryModel(TimestampMixin, Base):
    """跨会话的长期记忆：用户是谁、习惯什么、有什么长期约束。

    **和 `chat_sessions.memory_summary` 是两回事**，别混：

    - `memory_summary` 是**会话级短期记忆**——同一会话里被压缩掉的对话摘要，会话结束就没用了；
    - 这张表是**用户级长期记忆**——跨会话复用，"张工负责一号车间"这种下一轮对话还用得上的稳定事实。

    为什么自己建表而不用 LangGraph 的 ``BaseStore``：store 的表里**没有 tenant_id / user_id**，
    多租户隔离做不到 SQL 层；而且 ToB 场景下"用户能查看和删除自己的记忆"是必选项，
    把记忆藏在框架自管的表里还得再写一层映射。

    ``status`` 的存在同样是为了**可撤销**：模型提炼新记忆时会一并看到已有记忆，可以标出
    "这条过时了"（用户换岗了），而不是只能一味新增——没有作废机制，记忆库很快就是一堆自相矛盾的话。
    """

    __tablename__ = "user_memories"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('preference', 'fact', 'constraint')", name="kind_allowed"
        ),
        CheckConstraint("status IN ('active', 'archived')", name="status_allowed"),
        Index("ix_user_memories_user_status", "user_id", "status", "updated_at"),
    )

    id: Mapped[BigIntPrimaryKey]
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 从哪次会话提炼出来的（会话删了就置空，记忆本身留着）
    source_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
