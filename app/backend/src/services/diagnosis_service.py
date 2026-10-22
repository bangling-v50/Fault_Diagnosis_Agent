from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.backend.src.agents.prompts import KIND_LABELS
from app.backend.src.agents.state import DiagnosisState
from app.backend.src.agents.workflow import DiagnosisDeps, build_diagnosis_graph
from app.backend.src.background_jobs.runtime import BackgroundJobContext, JobCancelled
from app.backend.src.models import (
    AlertModel,
    BackgroundJobModel,
    DeviceModel,
    DiagnosisTaskModel,
    UserModel,
)
from app.backend.src.models.diagnosis import (
    DiagnosisEvidenceModel,
    DiagnosisReportModel,
    DiagnosisStepModel,
    ReportEvidenceLinkModel,
)
from app.backend.src.repositories.diagnoses import (
    DiagnosisEvidenceRepository,
    DiagnosisReportRepository,
    DiagnosisStepRepository,
    DiagnosisTaskRepository,
    EvidenceDraft,
    ReportEvidenceLinkRepository,
)
from app.backend.src.services.job_service import JobService

# 诊断成功后告警推进到的状态。``alerts.status`` 没有数据库级 CHECK，状态机靠约定
# （见 repositories/alerts.py 的 DEFAULT_OPEN_STATUSES）。
DIAGNOSED_ALERT_STATUS = "DIAGNOSED"


@dataclass(frozen=True, slots=True)
class DiagnosisBrief:
    """一次诊断的结论摘要（聊天工具的输出素材）。

    字段全是**可 JSON 序列化的原始类型**，理由同 ``WorkOrderHistoryItem``：它要么进提示词，
    要么进 ``tool_call_audits.result_payload``（JSONB），塞 ``datetime`` 会在写库时炸。
    """

    task_id: int
    status: str
    alert_code: str | None
    report_title: str | None
    risk_level: str | None
    summary: str | None
    possible_causes: list[str]
    suggested_actions: list[str]


@dataclass(frozen=True, slots=True)
class DiagnosisDetailData:
    """一次诊断的完整过程（详情接口的数据源）。

    ``evidence`` 是 ``(证据行, 引用编号)`` 的二元组列表——编号**故意在这里算**，因为它的
    唯一依据是"证据按 id 升序"这个查询顺序（见 ``DiagnosisEvidenceRepository.list_by_task``）。
    把编号和查询放在一起，将来谁改查询顺序都会立刻看见这段注释，而不是让编号悄悄错位。
    """

    task: DiagnosisTaskModel
    steps: list[DiagnosisStepModel]
    evidence: list[tuple[DiagnosisEvidenceModel, int]]  # (证据, 引用编号)
    reports: list[DiagnosisReportModel]
    links: list[ReportEvidenceLinkModel]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class DiagnosisService:
    def __init__(
        self,
        diagnosis_repo: DiagnosisTaskRepository,
        job_service: JobService,
        *,
        step_repo: DiagnosisStepRepository,
        evidence_repo: DiagnosisEvidenceRepository,
        report_repo: DiagnosisReportRepository,
        link_repo: ReportEvidenceLinkRepository,
    ) -> None:
        self._diagnosis_repo = diagnosis_repo
        self._job_service = job_service
        # 下面四个只在"读"路径（列表/详情）里用；写路径由 DiagnosisRunner 直接持有它们
        self._step_repo = step_repo
        self._evidence_repo = evidence_repo
        self._report_repo = report_repo
        self._link_repo = link_repo

    async def create_task_for_alert(
        self,
        alert: AlertModel,
        device: DeviceModel,
        trigger_type: str = "auto",
    ) -> tuple[DiagnosisTaskModel, BackgroundJobModel]:
        """建诊断任务并入队。

        ``thread_id`` 由仓储层在插入前生成（``diagnosis:{uuid4}``），这里不传。
        入队与建任务写在同一个方法里，是为了**结构上杜绝"建了任务却忘了入队"**——
        那会留下永远停在 QUEUED 的孤儿任务。
        """
        task = await self._diagnosis_repo.create_task(
            tenant_id=alert.tenant_id,
            device_id=device.id,
            alert_id=alert.id,
            trigger_type=trigger_type,
            status="QUEUED",
            input_payload=self._build_input_payload(alert=alert, device=device),
        )

        job = await self._job_service.enqueue(
            kind="diagnosis",
            resource_type="diagnosis_task",
            resource_id=task.id,
            tenant_id=alert.tenant_id,
            payload={
                "taskId": task.id,
                "threadId": task.thread_id,
                "alertId": alert.id,
                "deviceCode": device.device_code,
            },
        )
        return task, job

    # ---------- 查询（带租户作用域，规则同告警域） ----------

    async def list_tasks(
        self,
        actor: UserModel,
        *,
        page: int,
        page_size: int,
        tenant_id: int | None = None,
        device_id: int | None = None,
        alert_id: int | None = None,
        status: str | None = None,
    ) -> tuple[list[tuple[DiagnosisTaskModel, str | None, str | None]], int]:
        """分页列出诊断任务，返回 ``([(任务, 设备编码, 告警码), …], total)``。"""
        scoped_tenant_id = self._scope_tenant(actor, tenant_id)
        return await self._diagnosis_repo.list(
            page=page,
            page_size=page_size,
            tenant_id=scoped_tenant_id,
            device_id=device_id,
            alert_id=alert_id,
            status=status,
        )

    async def get_task_detail(self, actor: UserModel, task_id: int) -> DiagnosisDetailData:
        """取一个诊断任务的完整过程（任务 + 步骤 + 证据 + 报告 + 引用边）。"""
        task = await self._diagnosis_repo.find_by_id(task_id)
        if task is None:
            raise DiagnosisError("DIAGNOSIS_TASK_NOT_FOUND", f"诊断任务不存在：id={task_id}")
        self._ensure_visible(actor, task)

        # 三张子表互不依赖，并发取（纯读，不涉及顺序与事务）
        steps, evidence_rows, reports = await asyncio.gather(
            self._step_repo.list_by_task(task_id),
            self._evidence_repo.list_by_task(task_id),
            self._report_repo.list_by_task(task_id),
        )
        # 引用边挂在报告版本上，所以得等报告查完
        links = await self._link_repo.list_by_report_ids([report.id for report in reports])
        return DiagnosisDetailData(
            task=task,
            steps=steps,
            # 编号 = 下标 + 1：证据已按 id 升序（= 落盘顺序），与报告里的 [n] 同源
            evidence=[(row, index + 1) for index, row in enumerate(evidence_rows)],
            reports=reports,
            links=links,
        )

    # ---------- 给聊天 Agent 当工具用 ----------

    async def recent_diagnoses(
        self, *, tenant_id: int, device_id: int | None = None, limit: int = 3
    ) -> list[DiagnosisBrief]:
        """最近若干次诊断的**结论摘要**（含最终报告的关键字段）。

        给聊天 Agent 用：用户问"这台设备以前诊断过几次、都说是什么原因"时，一次就能答上来。
        作用域由工具闭包固定（租户必给、设备可选），所以这里不收 actor。
        """
        rows, _ = await self._diagnosis_repo.list(
            page=1,
            page_size=max(1, limit),
            tenant_id=tenant_id,
            device_id=device_id,
        )
        briefs: list[DiagnosisBrief] = []
        for task, _device_code, alert_code in rows:
            reports = await self._report_repo.list_by_task(task.id)
            report = next((item for item in reversed(reports) if item.is_final), None)
            briefs.append(
                DiagnosisBrief(
                    task_id=task.id,
                    status=task.status,
                    alert_code=alert_code,
                    report_title=report.title if report else None,
                    risk_level=report.risk_level if report else None,
                    summary=report.summary if report else None,
                    possible_causes=[
                        str(item.get("cause"))
                        for item in (report.possible_causes if report else [])
                        if item.get("cause")
                    ],
                    suggested_actions=[
                        str(item.get("action"))
                        for item in (report.suggested_actions if report else [])
                        if item.get("action")
                    ],
                )
            )
        return briefs

    # ---------- 内部：租户作用域 ----------

    def _scope_tenant(self, actor: UserModel, requested_tenant_id: int | None) -> int | None:
        """operator 只能看本租户；engineer/admin 可跨租户（engineer 按需过滤）。

        与 ``alert_service`` / ``device_service`` 里那两份逻辑相同——域一多就该抽到公共处
        （如 ``utils/scoping.py``），现在继续就近可读。
        """
        if actor.role == "operator":
            if actor.tenant_id is None:
                raise DiagnosisError("FORBIDDEN", "账号未绑定租户，无法查看诊断任务")
            if requested_tenant_id is not None and requested_tenant_id != actor.tenant_id:
                raise DiagnosisError("FORBIDDEN", "无权查看其他租户的诊断任务")
            return actor.tenant_id
        return requested_tenant_id

    def _ensure_visible(self, actor: UserModel, task: DiagnosisTaskModel) -> None:
        if actor.role == "operator" and task.tenant_id != actor.tenant_id:
            raise DiagnosisError("FORBIDDEN", "无权查看其他租户的诊断任务")

    @staticmethod
    def _build_input_payload(alert: AlertModel, device: DeviceModel) -> dict:
        """触发时的**快照**：workflow 与回放都从这里取值，避免执行期间源数据被改动。

        ⚠️ 必须是"纯 JSON"能序列化的值——JSONB 列用的是默认 ``json.dumps``，
        直接塞 ``datetime`` 会在写库时报
        ``TypeError: Object of type datetime is not JSON serializable``，
        所以时间统一转成 ISO 字符串。
        """
        return {
            "alertId": alert.id,
            "tenantId": alert.tenant_id,
            "alert": {
                "code": alert.alert_code,
                "name": alert.alert_name,
                "level": alert.alert_level,
                "status": alert.status,
                "source": alert.source,
                "rawMessage": alert.raw_message,
                "sensorSummary": alert.sensor_summary,
                "occurrenceCount": alert.occurrence_count,
                "firstSeenAt": _iso(alert.first_seen_at),
                "lastSeenAt": _iso(alert.last_seen_at),
            },
            "device": {
                "id": device.id,
                "deviceCode": device.device_code,
                "deviceName": device.device_name,
                "deviceModel": device.device_model,
                "serialNumber": device.serial_number,
                "installLocation": device.install_location,
            },
            "triggeredAt": datetime.now(timezone.utc).isoformat(),
        }


# 图里用 add reducer 累加的 key：它们的"增量"只是自己那一段，合并进 summary 会以偏概全，
# 所以由 runner 单独累加（见 run_task）。其余 key 是覆盖语义，直接 update 就是最终值。
_ACCUMULATED_KEYS = frozenset({"evidence_index", "collection_notes", "rejections", "events"})
# 进图后的第一个节点名，同时也是任务的第一阶段
_FIRST_PHASE = "collect_knowledge"


class DiagnosisError(Exception):
    """携带机器可读错误码的领域异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DiagnosisRunner:
    """worker 侧：把一条诊断任务真的跑完。

    与 ``DiagnosisService`` 不同，这个服务跑在**后台 worker** 里，入口收
    ``BackgroundJobContext``——靠它可以写进度事件（前端能回放）并响应协作式取消。

    它**不做业务判断**，只做三件事：准备状态 → 驱动图、把每个节点的事件转发出去并推进
    ``current_phase`` → 按结果收尾（任务状态 + 告警状态）。
    """

    def __init__(self, *, task_repo: DiagnosisTaskRepository, deps: DiagnosisDeps) -> None:
        self._task_repo = task_repo
        self._deps = deps

    async def run_task(self, context: BackgroundJobContext, *, task_id: int) -> None:
        task = await self._task_repo.find_by_id(task_id)
        if task is None:
            raise DiagnosisError("DIAGNOSIS_TASK_NOT_FOUND", f"诊断任务不存在：id={task_id}")

        # 设备与告警用**触发时的快照**（入队时就存在 input_payload 里）：执行期间源数据被改，
        # 也不会让本次诊断的输入与报告对不上。
        snapshot = task.input_payload or {}
        alert = snapshot.get("alert") or {}
        device = snapshot.get("device") or {}
        if not alert or not device:
            raise DiagnosisError(
                "DIAGNOSIS_INPUT_MISSING", f"诊断任务 {task_id} 的触发快照不完整，无法执行"
            )

        await self._task_repo.mark_running(task_id, phase=_FIRST_PHASE)
        state: DiagnosisState = {
            "task_id": task.id,
            "tenant_id": task.tenant_id,
            "device_id": task.device_id,
            # 带上 alertId：查历史告警时要靠它把"本次这条"排除掉，它自己不算历史
            "alert": {**alert, "alertId": snapshot.get("alertId") or task.alert_id},
            "device": device,
            "evidence_index": await self._seed_alert_evidence(task, alert=alert),
            "events": [],
        }
        graph = build_diagnosis_graph(self._deps, context=context)

        merged: dict[str, Any] = {}
        evidence_count = 0
        notes: list[str] = []
        try:
            # stream_mode="updates"：每跑完一个节点，yield 一次 {节点名: 该节点的增量}
            async for update in graph.astream(state, stream_mode="updates"):
                for node_name, node_update in update.items():
                    if not isinstance(node_update, dict):
                        continue
                    await self._task_repo.set_phase(task.id, node_name)
                    for event in node_update.get("events") or []:
                        await context.append_event(event)
                    evidence_count += len(node_update.get("evidence_index") or [])
                    notes.extend(node_update.get("collection_notes") or [])
                    merged.update(
                        {
                            key: value
                            for key, value in node_update.items()
                            if key not in _ACCUMULATED_KEYS
                        }
                    )
            await context.raise_if_cancelled()
        except JobCancelled:
            # 取消不是失败：任务有自己的 CANCELLED 状态，别混进 FAILED 里
            await self._task_repo.mark_cancelled(task.id, reason="任务已被请求取消")
            raise
        except Exception as exc:  # noqa: BLE001 —— 交给 runtime 决定退避重试还是终态失败
            await self._task_repo.mark_failed(task.id, error_message=_safe_error(exc))
            raise

        evaluation = merged.get("evaluation") or {}
        await self._task_repo.mark_succeeded(
            task.id,
            result_summary={
                "reportId": merged.get("report_id"),
                "reportVersion": merged.get("report_version"),
                "evaluation": evaluation,
                "evidenceCount": evidence_count,
                "collectionNotes": notes,
            },
        )
        # 告警推进到"已诊断"：否则 find_open_by_code（只认 NEW）会把后续同码上报一律
        # 合并进这条旧告警，同一种故障再也不会触发新诊断——闭环就断在这儿。
        await self._deps.alert_repo.update(task.alert_id, {"status": DIAGNOSED_ALERT_STATUS})
        await context.append_event(
            {
                "step": "done",
                "message": (
                    f"诊断完成：第 {merged.get('report_version')} 版报告，"
                    f"评审 {evaluation.get('verdict') or '未知'}"
                ),
            }
        )

    async def _seed_alert_evidence(
        self, task: DiagnosisTaskModel, *, alert: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """把"本次告警"本身落成第 1 条证据，作为进图时的初始证据。

        它不属于任何一步（``step_id`` 留空，该列可空）：这是**任务的输入**，不是哪个节点查出来的。
        编号让它在最前面，报告里说"依据本次告警的某某现象"时就有号可引。
        """
        summary = (
            f"告警码 {alert.get('code')}，级别 {alert.get('level')}，"
            f"累计出现 {alert.get('occurrenceCount') or 1} 次。"
            f"原始报文：{alert.get('rawMessage') or '无'}"
        )
        rows = await self._deps.evidence_repo.create_many(
            task_id=task.id,
            step_id=None,
            drafts=[
                EvidenceDraft(
                    kind="alert",
                    source_type="alert",
                    source_id=str(task.alert_id),
                    source_title=(
                        f"{alert.get('code') or ''} {alert.get('name') or ''}".strip() or None
                    ),
                    summary=summary,
                    payload=dict(alert),
                )
            ],
        )
        return [
            {
                "no": offset + 1,
                "evidence_id": row.id,
                "kind": row.kind,
                "kind_label": KIND_LABELS.get(row.kind, row.kind),
                "source_title": row.source_title,
                "summary": row.summary,
            }
            for offset, row in enumerate(rows)
        ]


# 把异常压成一行可入库的文本并截断（超长错误信息会把列撑爆）
def _safe_error(error: Exception) -> str:
    text = str(error).strip()
    return (text or error.__class__.__name__)[:1000]
