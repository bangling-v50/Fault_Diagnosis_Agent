from __future__ import annotations

from typing import TYPE_CHECKING

from app.backend.src.background_jobs.runtime import BackgroundJobContext, JobHandler

if TYPE_CHECKING:
    from app.backend.src.services.chat_service import ChatService
    from app.backend.src.services.diagnosis_service import DiagnosisRunner
    from app.backend.src.services.document_service import DocumentIndexingService


def build_chat_memory_extract_handler(service: ChatService) -> JobHandler:
    """把 ``ChatService`` 包成 ``chat_memory_extract`` 的 handler。"""

    async def handle(context: BackgroundJobContext) -> None:
        session_id = context.job.payload.get("sessionId") or context.job.resource_id
        await service.extract_memories_by_id(int(session_id))

    return handle


def build_diagnosis_handler(runner: DiagnosisRunner) -> JobHandler:
    """把 ``DiagnosisRunner`` 包成 ``diagnosis`` 的 handler。"""

    async def handle(context: BackgroundJobContext) -> None:
        task_id = context.job.payload.get("taskId") or context.job.resource_id
        await runner.run_task(context, task_id=int(task_id))

    return handle


def build_document_index_handler(service: DocumentIndexingService) -> JobHandler:
    """把 ``DocumentIndexingService`` 包成 ``document_index`` 的 handler。"""

    async def handle(context: BackgroundJobContext) -> None:
        task_id = context.job.payload.get("indexTaskId") or context.job.resource_id
        await service.run_task(context, task_id=int(task_id))

    return handle
