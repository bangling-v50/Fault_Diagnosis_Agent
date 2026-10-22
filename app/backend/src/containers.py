from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.backend.src.agents.workflow import DiagnosisDeps
from app.backend.src.background_jobs.handlers import (
    build_chat_memory_extract_handler,
    build_diagnosis_handler,
    build_document_index_handler,
)
from app.backend.src.background_jobs.runtime import BackgroundJobRuntime
from app.backend.src.documents.storage import ObjectStorage
from app.backend.src.llm_provider.chat import ChatModelProvider
from app.backend.src.llm_provider.embedding import EmbeddingModelProvider
from app.backend.src.llm_provider.rerank import RerankModelProvider
from app.backend.src.repositories.alerts import AlertRepository
from app.backend.src.repositories.audits import ToolCallAuditRepository
from app.backend.src.repositories.auth_sessions import AuthSessionRepository
from app.backend.src.repositories.chat import (
    ChatMessageRepository,
    ChatSessionRepository,
    UserMemoryRepository,
)
from app.backend.src.repositories.devices import DeviceRepository
from app.backend.src.repositories.diagnoses import (
    DiagnosisCaseRepository,
    DiagnosisEvidenceRepository,
    DiagnosisReportRepository,
    DiagnosisStepRepository,
    DiagnosisTaskRepository,
    ReportEvidenceLinkRepository,
)
from app.backend.src.repositories.documents import (
    DocumentIndexTaskRepository,
    DocumentRepository,
)
from app.backend.src.repositories.jobs import BackgroundJobRepository
from app.backend.src.repositories.tenants import TenantRepository
from app.backend.src.repositories.users import UserRepository
from app.backend.src.repositories.work_orders import WorkOrderRepository
from app.backend.src.services.alert_service import AlertService
from app.backend.src.services.auth_service import AuthService
from app.backend.src.services.case_service import CaseService
from app.backend.src.services.chat_agent import LangChainChatAgentRunner
from app.backend.src.services.chat_memory import ChatMemoryService
from app.backend.src.services.chat_service import ChatService
from app.backend.src.services.device_service import DeviceService
from app.backend.src.services.diagnosis_service import DiagnosisRunner, DiagnosisService
from app.backend.src.services.document_service import (
    DocumentIndexingService,
    DocumentService,
)
from app.backend.src.services.job_service import JobService
from app.backend.src.services.retrieval_service import RetrievalService
from app.backend.src.services.tenant_service import TenantService
from app.backend.src.services.user_service import UserService
from app.backend.src.services.work_order_service import WorkOrderService
from app.backend.src.settings import Settings
from app.backend.src.tools.chat import ChatToolDeps
from app.backend.src.vector_store.pgvector import PGVectorStore


class Container:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._engine: AsyncEngine = create_async_engine(
            settings.database_url,
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            pool_recycle=settings.pool_recycle_seconds,
        )
        # expire_on_commit=False：repository 方法提交后返回的 ORM 对象仍可读取属性
        self.session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

        # --- 持久化单例 ---
        self.tenant_repo = TenantRepository(self.session_factory)
        self.user_repo = UserRepository(self.session_factory)
        self.session_repo = AuthSessionRepository(self.session_factory)
        self.device_repo = DeviceRepository(self.session_factory)
        self.job_repo = BackgroundJobRepository(self.session_factory)
        self.alert_repo = AlertRepository(self.session_factory)
        self.diagnosis_repo = DiagnosisTaskRepository(self.session_factory)
        self.diagnosis_step_repo = DiagnosisStepRepository(self.session_factory)
        self.diagnosis_evidence_repo = DiagnosisEvidenceRepository(self.session_factory)
        self.diagnosis_report_repo = DiagnosisReportRepository(self.session_factory)
        self.report_evidence_link_repo = ReportEvidenceLinkRepository(self.session_factory)
        self.diagnosis_case_repo = DiagnosisCaseRepository(self.session_factory)
        self.chat_session_repo = ChatSessionRepository(self.session_factory)
        self.chat_message_repo = ChatMessageRepository(self.session_factory)
        self.user_memory_repo = UserMemoryRepository(self.session_factory)
        self.tool_audit_repo = ToolCallAuditRepository(self.session_factory)
        self.work_order_repo = WorkOrderRepository(self.session_factory)
        self.document_repo = DocumentRepository(self.session_factory)
        self.index_task_repo = DocumentIndexTaskRepository(self.session_factory)

        # --- 向量库与模型客户端 ---
        # 向量库改用 PostgreSQL + pgvector：它复用同一个 session_factory（同一个连接池、同一套事务），
        # 不再需要懒连接。放在业务单例之前：DocumentService（删除时要清向量）与
        # DocumentIndexingService 都依赖它。embedding 仍是懒构建（用到才建客户端）。
        # 文档实体存储（S3 兼容，MinIO / OSS / S3 同一套代码）：上传的原文件与解析副产物
        # 都放这里，服务器本地不留实体文件。bucket 在 start() 里确保存在。
        self.object_storage = ObjectStorage(
            endpoint=settings.object_storage_endpoint,
            access_key=settings.object_storage_access_key,
            secret_key=settings.object_storage_secret_key,
            bucket=settings.object_storage_bucket,
            region=settings.object_storage_region,
        )
        self.vector_store = PGVectorStore(self.session_factory, settings.vector_store)
        self.embedding_model = EmbeddingModelProvider(
            settings.llm, batch_size=settings.embedding_batch_size
        )
        # 聊天模型：诊断 workflow 里"写报告"和"当裁判"两处用它（同样是懒构建）
        self.chat_model = ChatModelProvider(settings.llm)
        self.document_indexing_service = DocumentIndexingService(
            document_repo=self.document_repo,
            index_task_repo=self.index_task_repo,
            vector_store=self.vector_store,
            embedding_model=self.embedding_model,
            storage=self.object_storage,
            settings=settings,
        )
        # 精排客户端（自己有 httpx 连接，所以也是单例）
        self.rerank_model = RerankModelProvider(settings.llm)
        self.retrieval_service = RetrievalService(
            vector_store=self.vector_store,
            embedding_model=self.embedding_model,
            rerank_model=self.rerank_model,
            min_similarity=settings.retrieval_min_similarity,
        )

        # --- 业务单例：构造注入持久化 ---
        self.auth_service = AuthService(
            user_repo=self.user_repo,
            tenant_repo=self.tenant_repo,
            session_repo=self.session_repo,
            session_ttl_seconds=settings.session_ttl_seconds,
        )
        self.tenant_service = TenantService(tenant_repo=self.tenant_repo)
        self.user_service = UserService(
            user_repo=self.user_repo,
            tenant_repo=self.tenant_repo,
            session_repo=self.session_repo,
        )
        self.device_service = DeviceService(
            device_repo=self.device_repo,
            tenant_repo=self.tenant_repo,
        )
        self.job_service = JobService(job_repo=self.job_repo)
        self.diagnosis_service = DiagnosisService(
            diagnosis_repo=self.diagnosis_repo,
            job_service=self.job_service,
            step_repo=self.diagnosis_step_repo,
            evidence_repo=self.diagnosis_evidence_repo,
            report_repo=self.diagnosis_report_repo,
            link_repo=self.report_evidence_link_repo,
        )
        self.alert_service = AlertService(alert_repo=self.alert_repo,diagnosis_service=self.diagnosis_service)
        # 工单：既给诊断 Agent 提供「同设备 + 同告警码的历史工单」查询，也给界面提供建/派/结案
        self.work_order_service = WorkOrderService(
            work_order_repo=self.work_order_repo,
            device_repo=self.device_repo,
            user_repo=self.user_repo,
            alert_repo=self.alert_repo,
        )
        # 诊断 workflow 的依赖打包（图按次构建时把它传进去；里面全是无状态单例）
        self.diagnosis_deps = DiagnosisDeps(
            chat_model=self.chat_model,
            retrieval_service=self.retrieval_service,
            alert_repo=self.alert_repo,
            work_order_service=self.work_order_service,
            step_repo=self.diagnosis_step_repo,
            evidence_repo=self.diagnosis_evidence_repo,
            report_repo=self.diagnosis_report_repo,
            link_repo=self.report_evidence_link_repo,
        )
        self.diagnosis_runner = DiagnosisRunner(
            task_repo=self.diagnosis_repo, deps=self.diagnosis_deps
        )
        self.document_service = DocumentService(
            document_repo=self.document_repo,
            index_task_repo=self.index_task_repo,
            tenant_repo=self.tenant_repo,
            job_service=self.job_service,
            vector_store=self.vector_store,
            storage=self.object_storage,
            max_upload_bytes=settings.doc_max_upload_bytes,
        )
        # --- 聊天 Agent：编排 + 记忆 ---
        # agent runner 是唯一碰 LangChain 的一层（换框架或加 HITL 只换它）
        self.chat_agent_runner = LangChainChatAgentRunner(self.chat_model)
        self.chat_memory_service = ChatMemoryService(
            session_repo=self.chat_session_repo,
            message_repo=self.chat_message_repo,
            memory_repo=self.user_memory_repo,
            chat_model=self.chat_model,
            settings=settings,
        )
        self.chat_tool_deps = ChatToolDeps(
            retrieval_service=self.retrieval_service,
            work_order_service=self.work_order_service,
            diagnosis_service=self.diagnosis_service,
            alert_service=self.alert_service,
        )
        self.chat_service = ChatService(
            session_repo=self.chat_session_repo,
            message_repo=self.chat_message_repo,
            memory_repo=self.user_memory_repo,
            audit_repo=self.tool_audit_repo,
            device_repo=self.device_repo,
            memory_service=self.chat_memory_service,
            runner=self.chat_agent_runner,
            tool_deps=self.chat_tool_deps,
            job_service=self.job_service,
            settings=settings,
        )

        # 案例沉淀：把已结案工单固化成案例，并借文档域的 create_from_text 回流成知识文档。
        # 必须排在 document_service 之后——它依赖那个单例（同一个落盘/入队通道）。
        self.case_service = CaseService(
            case_repo=self.diagnosis_case_repo,
            work_order_repo=self.work_order_repo,
            device_repo=self.device_repo,
            alert_repo=self.alert_repo,
            report_repo=self.diagnosis_report_repo,
            document_service=self.document_service,
        )

        # --- 后台任务运行时：handler 注册表 ---
        self.job_runtime = BackgroundJobRuntime(
            self.job_repo,
            concurrency=settings.job_worker_concurrency,
            lease_seconds=settings.job_lease_seconds,
            poll_seconds=settings.job_poll_seconds,
        )
        # diagnosis：真实的 langgraph 诊断 workflow（三固定工具 + 评估回环）
        self.job_runtime.register("diagnosis", build_diagnosis_handler(self.diagnosis_runner))
        # document_index：真实索引（抽取 → 切分 → 嵌入 → 写 pgvector）
        self.job_runtime.register(
            "document_index", build_document_index_handler(self.document_indexing_service)
        )
        self.job_runtime.register(
            "chat_memory_extract", build_chat_memory_extract_handler(self.chat_service)
        )

    async def start(self) -> None:
        """启动前的异步装配 + 启动后台 worker（FastAPI lifespan 启动阶段调用）。"""
        await asyncio.to_thread(self.object_storage.ensure_bucket)
        await self.job_runtime.start()

    async def ping(self) -> None:
        """就绪检查：确认数据库可连通。"""
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def aclose(self) -> None:
        await self.job_runtime.stop()
        await self.vector_store.close()
        await self.rerank_model.close()
        await self._engine.dispose()
