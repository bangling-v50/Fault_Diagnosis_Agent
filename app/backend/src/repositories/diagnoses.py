from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backend.src.models.alert import AlertModel
from app.backend.src.models.diagnosis import (
    DiagnosisCaseModel,
    DiagnosisEvidenceModel,
    DiagnosisReportModel,
    DiagnosisStepModel,
    DiagnosisTaskModel,
    ReportEvidenceLinkModel,
)
from app.backend.src.models.equipment import DeviceModel


@dataclass(frozen=True, slots=True)
class EvidenceDraft:
    """待写入的一条证据。

    ``kind`` 受数据库 CHECK 限制，只能是 ``alert`` / ``knowledge`` /
    ``alert_history`` / ``work_order``；``source_type`` + ``source_id`` 指向**具体是哪个
    源对象**（告警 id / 片段 id / 工单 id），报告引用和溯源都靠它们。
    """

    kind: str
    source_type: str
    source_id: str
    summary: str
    source_title: str | None = None
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CaseDraft:
    """待写入的一条沉淀案例。

    ``scope='global'`` 时 ``tenant_id`` 必须为 None，反之必须有值——数据库有 CHECK 兜底
    （``scope_matches_tenant``），这里提前对齐，免得撞上去才发现。
    """

    scope: str
    tenant_id: int | None
    device_model: str
    alert_code: str | None
    source_work_order_id: int
    root_cause: str
    solution: str
    resolution_result: str
    summary: str
    case_fingerprint: str
    source_task_id: int | None = None
    source_report_id: int | None = None
    keywords: list[str] = field(default_factory=list)
    document_id: int | None = None
    index_task_id: int | None = None


class DiagnosisTaskRepository:
    """``diagnosis_tasks``：建任务 + 生命周期状态推进。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_task(
        self,
        *,
        tenant_id: int,
        device_id: int,
        alert_id: int,
        input_payload: dict,
        trigger_type: str = "auto",
        status: str = "QUEUED",
    ) -> DiagnosisTaskModel:
        """建一条诊断任务。

        三个 NOT NULL 外键 ``tenant_id`` / ``device_id`` / ``alert_id`` 必传；此外：

        - ``thread_id`` 是 LangGraph 的 thread 标识（unique），在**插入前**生成
          ``diagnosis:{uuid4}``——它是 NOT NULL 列，必须随 INSERT 一起给出；
          不能"先插入拿 id 再回填"，那样第一次 INSERT 就会因 NULL 被数据库拒绝；
        - ``input_payload`` 是**非空** JSONB，建议存一份触发时的告警快照，便于回放与排错；
        - ``trigger_type`` 受 CHECK 约束：``auto`` / ``manual`` / ``retry``。

        其余生命周期字段（``current_phase`` / ``result_summary`` / ``error_message`` /
        ``started_at`` / ``completed_at``）由 workflow 与 worker 在执行过程中回填，创建时不设。
        """
        row = DiagnosisTaskModel(
            tenant_id=tenant_id,
            device_id=device_id,
            alert_id=alert_id,
            thread_id=f"diagnosis:{uuid4().hex}",
            trigger_type=trigger_type,
            status=status,
            input_payload=input_payload,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
        return row

    async def find_by_id(self, task_id: int) -> DiagnosisTaskModel | None:
        async with self._session_factory() as session:
            return await session.get(DiagnosisTaskModel, task_id)

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        tenant_id: int | None = None,
        device_id: int | None = None,
        alert_id: int | None = None,
        status: str | None = None,
    ) -> tuple[list[tuple[DiagnosisTaskModel, str | None, str | None]], int]:
        """分页列出诊断任务，**顺带 join 出设备编码与告警码**。

        为什么要 join：``diagnosis_tasks`` 只有 ``device_id`` / ``alert_id``，而列表上要显示
        "哪台设备、什么告警"。让前端拿着 id 逐条回查就是 N+1；这里一次 join 拿完。

        返回 ``([(任务, 设备编码, 告警码), …], total)``——三元组顺序与 select 的列顺序一致。
        """
        conditions = []
        if tenant_id is not None:
            conditions.append(DiagnosisTaskModel.tenant_id == tenant_id)
        if device_id is not None:
            conditions.append(DiagnosisTaskModel.device_id == device_id)
        if alert_id is not None:
            conditions.append(DiagnosisTaskModel.alert_id == alert_id)
        if status:
            conditions.append(DiagnosisTaskModel.status == status)

        joined = (
            select(DiagnosisTaskModel, DeviceModel.device_code, AlertModel.alert_code)
            .join(DeviceModel, DiagnosisTaskModel.device_id == DeviceModel.id)
            .join(AlertModel, DiagnosisTaskModel.alert_id == AlertModel.id)
            .where(*conditions)
        )
        count_stmt = (
            select(func.count()).select_from(DiagnosisTaskModel).where(*conditions)
        )
        page_stmt = (
            joined.order_by(DiagnosisTaskModel.created_at.desc(), DiagnosisTaskModel.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        async with self._session_factory() as session:
            total = int((await session.scalar(count_stmt)) or 0)
            results = (await session.execute(page_stmt)).all()
        return [(task, device_code, alert_code) for task, device_code, alert_code in results], total

    async def mark_running(self, task_id: int, *, phase: str) -> DiagnosisTaskModel | None:
        """进入执行：置 RUNNING、记开始时间与当前阶段。

        同时清掉上一次尝试留下的 ``error_message`` / ``completed_at``——job 重试时会再走一遍
        这里，残留的旧错误会让人误以为这次也失败了。
        """
        return await self._update(
            task_id,
            {
                "status": "RUNNING",
                "current_phase": phase,
                "started_at": _utc_now(),
                "completed_at": None,
                "error_message": None,
            },
        )

    async def set_phase(self, task_id: int, phase: str) -> DiagnosisTaskModel | None:
        """推进当前阶段（前端"正在检索知识库…"这类提示靠它）。"""
        return await self._update(task_id, {"current_phase": phase})

    async def mark_succeeded(
        self, task_id: int, *, result_summary: dict
    ) -> DiagnosisTaskModel | None:
        return await self._update(
            task_id,
            {
                "status": "SUCCEEDED",
                "completed_at": _utc_now(),
                "result_summary": result_summary,
                "error_message": None,
            },
        )

    async def mark_failed(self, task_id: int, *, error_message: str) -> DiagnosisTaskModel | None:
        return await self._update(
            task_id,
            {"status": "FAILED", "completed_at": _utc_now(), "error_message": error_message[:1000]},
        )

    async def mark_cancelled(self, task_id: int, *, reason: str) -> DiagnosisTaskModel | None:
        """被协作式取消：任务有自己的 CANCELLED 状态，别混进 FAILED 里。"""
        return await self._update(
            task_id,
            {"status": "CANCELLED", "completed_at": _utc_now(), "error_message": reason[:1000]},
        )

    async def _update(self, task_id: int, fields: dict) -> DiagnosisTaskModel | None:
        async with self._session_factory() as session:
            row = await session.get(DiagnosisTaskModel, task_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            await session.commit()
            await session.refresh(row)  # onupdate=func.now() 会让 updated_at 失效，须刷新
        return row


class DiagnosisStepRepository:
    """``diagnosis_steps``：每走完一个节点追加一行（**每步立即提交**）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def append(
        self, *, task_id: int, phase: str, status: str, payload: dict
    ) -> DiagnosisStepModel:
        """追加一步。

        ``sequence`` 不是计数器，而是插入时现算 ``MAX(sequence)+1``——同一个任务只有
        一个 worker 在写（租约保证），不会撞车；万一日后并发，唯一约束
        ``uq_diagnosis_steps_task_sequence`` 会当场拒绝，不会静默写乱顺序。
        """
        async with self._session_factory() as session:
            current_max = await session.scalar(
                select(func.coalesce(func.max(DiagnosisStepModel.sequence), 0)).where(
                    DiagnosisStepModel.task_id == task_id
                )
            )
            row = DiagnosisStepModel(
                task_id=task_id,
                sequence=int(current_max or 0) + 1,
                phase=phase,
                status=status,
                payload=payload,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(row)
        return row

    async def list_by_task(self, task_id: int) -> list[DiagnosisStepModel]:
        """一个任务的全部步骤，按 ``sequence`` 升序（= 执行顺序）。"""
        stmt = (
            select(DiagnosisStepModel)
            .where(DiagnosisStepModel.task_id == task_id)
            .order_by(DiagnosisStepModel.sequence)
        )
        async with self._session_factory() as session:
            return list((await session.scalars(stmt)).all())


class DiagnosisEvidenceRepository:
    """``diagnosis_evidence``：一条证据 = 一个可指认的源对象。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_many(
        self,
        *,
        task_id: int,
        step_id: int | None,
        drafts: Sequence[EvidenceDraft],
    ) -> list[DiagnosisEvidenceModel]:
        """一次写入一组证据（通常是一个节点产出的那几条），返回落库后的行。

        ``tool_call_id`` 留空：``tool_call_audits`` 的写入属于"审计留痕"那一步，
        现在没有审计行可指（列可空，不影响证据链）。
        """
        if not drafts:
            return []
        rows: list[DiagnosisEvidenceModel] = [
            DiagnosisEvidenceModel(
                task_id=task_id,
                step_id=step_id,
                kind=draft.kind,
                source_type=draft.source_type,
                source_id=draft.source_id,
                source_title=draft.source_title,
                summary=draft.summary,
                payload=draft.payload,
            )
            for draft in drafts
        ]
        async with self._session_factory() as session:
            session.add_all(rows)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            for row in rows:
                await session.refresh(row)
        return rows

    async def list_by_task(self, task_id: int) -> list[DiagnosisEvidenceModel]:
        """一个任务的全部证据，**按 ``id`` 升序**。

        ⚠️ 这个顺序不是随便定的：它是**报告里引用编号（``[1]`` ``[2]``）的唯一依据**。
        写入时 ``create_many`` 是"add_all 后顺序提交"，``id`` 是 identity 递增，所以
        "按 id 排序" == "按落盘顺序" == 报告作者看到的那套编号。详情接口给每条证据算
        ``citation_no = 下标 + 1``，前端才能从报告里的 ``[2]`` 跳到第 2 条证据。
        """
        stmt = (
            select(DiagnosisEvidenceModel)
            .where(DiagnosisEvidenceModel.task_id == task_id)
            .order_by(DiagnosisEvidenceModel.id)
        )
        async with self._session_factory() as session:
            return list((await session.scalars(stmt)).all())


class DiagnosisReportRepository:
    """``diagnosis_reports``：版本化的报告。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_version(
        self,
        *,
        task_id: int,
        title: str | None,
        summary: str | None,
        risk_level: str | None,
        possible_causes: list[dict],
        suggested_actions: list[dict],
        need_shutdown: str | None,
        need_field_service: bool | None,
        content: str,
        payload: dict,
        llm_model: str | None,
    ) -> DiagnosisReportModel:
        """写入新一版报告（``version`` 自动取"现有版本数 + 1"）。

        同一个事务里**先把旧版本置为 ``is_final=false`` 再插新版**，维护住
        "最新一版是 final"这个不变量——分开写会出现"两版都 final"的中间态。
        """
        async with self._session_factory() as session:
            existing = await session.scalar(
                select(func.count())
                .select_from(DiagnosisReportModel)
                .where(DiagnosisReportModel.task_id == task_id)
            )
            # ⚠️ 顺序不能反：先撤旧版本的 final，再插入新行。反过来的话，SQLAlchemy 在执行
            # 这条 UPDATE 前会先把新行 flush 进库，那条 `WHERE is_final = true` 会连**刚插入的
            # 新版本一起**置成 false，结果一个 final 都不剩（这个坑很安静，不会报错）。
            await session.execute(
                update(DiagnosisReportModel)
                .where(
                    DiagnosisReportModel.task_id == task_id,
                    DiagnosisReportModel.is_final.is_(True),
                )
                .values(is_final=False)
                .execution_options(synchronize_session=False)
            )
            row = DiagnosisReportModel(
                task_id=task_id,
                version=int(existing or 0) + 1,
                is_final=True,
                title=title,
                summary=summary,
                risk_level=risk_level,
                possible_causes=possible_causes,
                suggested_actions=suggested_actions,
                need_shutdown=need_shutdown,
                need_field_service=need_field_service,
                content=content,
                payload=payload,
                llm_model=llm_model,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(row)
        return row

    async def find_by_id(self, report_id: int) -> DiagnosisReportModel | None:
        """按 id 取一版报告（案例沉淀要从报告倒推出它属于哪次诊断任务）。"""
        async with self._session_factory() as session:
            return await session.get(DiagnosisReportModel, report_id)

    async def list_by_task(self, task_id: int) -> list[DiagnosisReportModel]:
        """一个任务的全部报告版本，按 ``version`` 升序（最后一版是 ``is_final``）。"""
        stmt = (
            select(DiagnosisReportModel)
            .where(DiagnosisReportModel.task_id == task_id)
            .order_by(DiagnosisReportModel.version)
        )
        async with self._session_factory() as session:
            return list((await session.scalars(stmt)).all())

    async def set_review(self, report_id: int, *, review: dict) -> DiagnosisReportModel | None:
        """把评审结论写回报告行（``payload.review``）。

        报告是**先写、后评**的：写报告那一刻还不知道自己会被判什么，所以评审结论只能回填。
        放在这里而不是"等跑完一起写"，是为了让"这份报告被评成什么样"和报告本身始终能一起读到。

        ⚠️ 正文里那句"未通过评审"的提示语**不在这个方法里**——它由调用方
        （``evaluate_report``）在确定最终结果后用 :meth:`update_content` 补写。
        本方法只管评审结论这一列。
        """
        async with self._session_factory() as session:
            row = await session.get(DiagnosisReportModel, report_id)
            if row is None:
                return None
            # 整块赋值（而不是 row.payload["review"] = ...）：JSONB 不会追踪容器内部改动
            row.payload = {**(row.payload or {}), "review": review}
            await session.commit()
            await session.refresh(row)
        return row

    async def update_content(self, report_id: int, *, content: str) -> DiagnosisReportModel | None:
        """只更新报告正文。

        唯一调用方是 ``evaluate_report``：判 fail 且额度用尽（不再重写）时，往正文顶部
        补一句"本报告未通过自动评审"。

        为什么不塞进 :meth:`set_review`：那个方法的职责是"回填评审结论"，改正文是另一件事，
        而且两者写的是不同的列（``payload`` vs ``content``）。合成一个方法会让
        "正文是在哪一步被改的"变得难以追查。
        """
        async with self._session_factory() as session:
            row = await session.get(DiagnosisReportModel, report_id)
            if row is None:
                return None
            row.content = content
            await session.commit()
            await session.refresh(row)
        return row


class ReportEvidenceLinkRepository:
    """``report_evidence_links``：报告 → 证据的引用边。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def link_many(
        self, *, report_id: int, links: Sequence[tuple[int, str | None]]
    ) -> list[ReportEvidenceLinkModel]:
        """按顺序建立引用边，``citation_order`` 从 1 起。

        ``links`` 是 ``(evidence_id, claim_key)`` 序列；``claim_key`` 记这条证据撑的是哪个结论
        （如 ``possibleCause#1``），可以不填。同一条证据重复引用只留**第一次**出现的位置——
        表上有 ``uq_report_evidence_links_report_evidence``，重复插会直接报错。
        """
        if not links:
            return []
        seen: set[int] = set()
        rows: list[ReportEvidenceLinkModel] = []
        for evidence_id, claim_key in links:
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            rows.append(
                ReportEvidenceLinkModel(
                    report_id=report_id,
                    evidence_id=evidence_id,
                    citation_order=len(rows) + 1,
                    claim_key=claim_key,
                )
            )
        async with self._session_factory() as session:
            session.add_all(rows)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            for row in rows:
                await session.refresh(row)
        return rows

    async def list_by_report_ids(
        self, report_ids: Sequence[int]
    ) -> list[ReportEvidenceLinkModel]:
        """若干报告版本的引用边，按 ``(report_id, citation_order)`` 升序。

        一个任务最多两版报告，所以按"报告列表"一次取完，不做逐版本查询。
        """
        if not report_ids:
            return []
        stmt = (
            select(ReportEvidenceLinkModel)
            .where(ReportEvidenceLinkModel.report_id.in_(list(report_ids)))
            .order_by(ReportEvidenceLinkModel.report_id, ReportEvidenceLinkModel.citation_order)
        )
        async with self._session_factory() as session:
            return list((await session.scalars(stmt)).all())


class DiagnosisCaseRepository:
    """``diagnosis_cases``：工程师确认过的维修案例（会回流成知识文档）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, draft: CaseDraft, *, confirmed_by: int) -> DiagnosisCaseModel:
        """建一条案例。``confirmed_by`` / ``confirmed_at`` 都是 NOT NULL——案例必须有人背书。"""
        row = DiagnosisCaseModel(
            tenant_id=draft.tenant_id,
            scope=draft.scope,
            device_model=draft.device_model,
            alert_code=draft.alert_code,
            source_work_order_id=draft.source_work_order_id,
            source_task_id=draft.source_task_id,
            source_report_id=draft.source_report_id,
            root_cause=draft.root_cause,
            solution=draft.solution,
            resolution_result=draft.resolution_result,
            summary=draft.summary,
            keywords=draft.keywords,
            case_fingerprint=draft.case_fingerprint,
            document_id=draft.document_id,
            index_task_id=draft.index_task_id,
            confirmed_by=confirmed_by,
            confirmed_at=_utc_now(),
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(row)
        return row

    async def find_by_id(self, case_id: int) -> DiagnosisCaseModel | None:
        async with self._session_factory() as session:
            return await session.get(DiagnosisCaseModel, case_id)

    async def find_by_fingerprint(self, fingerprint: str) -> DiagnosisCaseModel | None:
        """按内容指纹找案例（``case_fingerprint`` 是唯一的）。"""
        stmt = select(DiagnosisCaseModel).where(
            DiagnosisCaseModel.case_fingerprint == fingerprint
        )
        async with self._session_factory() as session:
            return (await session.scalars(stmt)).one_or_none()

    async def find_by_work_order(self, work_order_id: int) -> DiagnosisCaseModel | None:
        """这张工单是否已经沉淀过案例（幂等判断用）。"""
        stmt = (
            select(DiagnosisCaseModel)
            .where(DiagnosisCaseModel.source_work_order_id == work_order_id)
            .order_by(DiagnosisCaseModel.id.desc())
            .limit(1)
        )
        async with self._session_factory() as session:
            return (await session.scalars(stmt)).one_or_none()

    async def set_document(
        self, case_id: int, *, document_id: int, index_task_id: int | None
    ) -> DiagnosisCaseModel | None:
        """回填案例生成的知识文档与索引任务（案例先建、文档后建，见 ``case_service``）。"""
        async with self._session_factory() as session:
            row = await session.get(DiagnosisCaseModel, case_id)
            if row is None:
                return None
            row.document_id = document_id
            row.index_task_id = index_task_id
            await session.commit()
            await session.refresh(row)
        return row

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        tenant_id: int | None = None,
        include_global: bool = True,
        device_model: str | None = None,
        alert_code: str | None = None,
        work_order_id: int | None = None,
    ) -> tuple[list[DiagnosisCaseModel], int]:
        """分页列表，可见性规则同文档域：

        ``tenant_id`` 给定时表示"某个租户可见的案例"——默认把 ``scope='global'`` 一并带上
        （全局案例对所有租户可见）；``tenant_id=None`` 表示不做租户限制（engineer/admin 跨租户）。
        """
        conditions = []
        if tenant_id is not None:
            tenant_cond = DiagnosisCaseModel.tenant_id == tenant_id
            if include_global:
                tenant_cond = or_(tenant_cond, DiagnosisCaseModel.scope == "global")
            conditions.append(tenant_cond)
        if device_model:
            conditions.append(DiagnosisCaseModel.device_model == device_model)
        if alert_code:
            conditions.append(DiagnosisCaseModel.alert_code == alert_code)
        if work_order_id is not None:
            # 工单详情页用它判断"这张工单沉淀了没有"
            conditions.append(DiagnosisCaseModel.source_work_order_id == work_order_id)

        count_stmt = select(func.count()).select_from(DiagnosisCaseModel).where(*conditions)
        page_stmt = (
            select(DiagnosisCaseModel)
            .where(*conditions)
            .order_by(DiagnosisCaseModel.created_at.desc(), DiagnosisCaseModel.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        async with self._session_factory() as session:
            total = int((await session.scalar(count_stmt)) or 0)
            rows = list((await session.scalars(page_stmt)).all())
        return rows, total


# 统一用带时区的 UTC 时间——库列是 timestamptz，混用 naive datetime 会报错
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
