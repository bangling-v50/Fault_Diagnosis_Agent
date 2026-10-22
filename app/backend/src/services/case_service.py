from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from app.backend.src.models import AlertModel, DeviceModel, UserModel
from app.backend.src.models.diagnosis import DiagnosisCaseModel
from app.backend.src.models.work_orders import WorkOrderModel
from app.backend.src.repositories.alerts import AlertRepository
from app.backend.src.repositories.devices import DeviceRepository
from app.backend.src.repositories.diagnoses import (
    CaseDraft,
    DiagnosisCaseRepository,
    DiagnosisReportRepository,
)
from app.backend.src.repositories.work_orders import CLOSED_STATUSES, WorkOrderRepository
from app.backend.src.schemas.document import DocumentUploadForm
from app.backend.src.services.document_service import DocumentService

# 与 knowledge_documents.source_type 的 CHECK 一致
CASE_SOURCE_TYPE = "diagnosis_case"
# 与 knowledge_documents.doc_type 的 CHECK 一致（知识库页正是按它筛出案例的）
CASE_DOC_TYPE = "diagnostic_case"
# 机型缺失时的占位：device_model 是 NOT NULL，宁可用占位也不要让沉淀整个失败
UNKNOWN_DEVICE_MODEL = "未知机型"
MAX_SUMMARY_CHARS = 240
MAX_KEYWORDS = 12


class CaseError(Exception):
    """携带机器可读错误码的领域异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CaseService:
    def __init__(
        self,
        *,
        case_repo: DiagnosisCaseRepository,
        work_order_repo: WorkOrderRepository,
        device_repo: DeviceRepository,
        alert_repo: AlertRepository,
        report_repo: DiagnosisReportRepository,
        document_service: DocumentService,
    ) -> None:
        self._case_repo = case_repo
        self._work_order_repo = work_order_repo
        self._device_repo = device_repo
        self._alert_repo = alert_repo
        # 只为了从来源报告倒推出诊断任务 id（案例 → 报告 → 任务，一条完整的溯源链）
        self._report_repo = report_repo
        # 案例文档走文档域的 create_from_text 通道（落盘 + 落库 + 建索引任务 + 入队都在那边）
        self._document_service = document_service

    async def create_case(
        self,
        actor: UserModel,
        *,
        work_order_id: int,
        root_cause: str,
        solution: str,
        resolution_result: str,
        keywords: list[str] | None = None,
        summary: str | None = None,
        scope: str = "tenant",
    ) -> tuple[DiagnosisCaseModel, bool]:
        """沉淀一条案例，返回 ``(案例, 是否本次新建)``。重复调用不会产生第二条。"""
        if scope not in ("tenant", "global"):
            raise CaseError("CASE_SCOPE_INVALID", "scope 只能是 tenant 或 global")

        order = await self._work_order_repo.find_by_id(work_order_id)
        if order is None:
            raise CaseError("WORK_ORDER_NOT_FOUND", f"工单不存在：id={work_order_id}")
        if order.status not in CLOSED_STATUSES:
            raise CaseError(
                "WORK_ORDER_NOT_CLOSED",
                f"工单当前状态为 {order.status}，只有已结案（RESOLVED/CLOSED）的维修才能沉淀为案例",
            )

        # 幂等路径一：这张工单已经沉淀过
        existing = await self._case_repo.find_by_work_order(work_order_id)
        if existing is not None:
            return await self._ensure_document(actor, existing, order), False

        device = await self._device_repo.find_by_id(order.device_id)
        alert = (
            await self._alert_repo.find_by_id(order.alert_id) if order.alert_id else None
        )
        fingerprint = _fingerprint(order, device, alert, root_cause, solution)

        # 幂等路径二：同样内容的案例已经存在（可能来自另一张工单）
        same = await self._case_repo.find_by_fingerprint(fingerprint)
        if same is not None:
            return same, False

        draft = CaseDraft(
            scope=scope,
            # global 案例必须没有租户（数据库 CHECK scope_matches_tenant 兜底）
            tenant_id=order.tenant_id if scope == "tenant" else None,
            device_model=_device_model_of(device),
            alert_code=alert.alert_code if alert else None,
            source_work_order_id=order.id,
            source_task_id=await self._resolve_task_id(order),
            source_report_id=order.diagnosis_report_id,
            root_cause=root_cause,
            solution=solution,
            resolution_result=resolution_result,
            summary=_truncate(summary) or _default_summary(order, root_cause),
            keywords=keywords or _derive_keywords(order.title, root_cause, solution),
            case_fingerprint=fingerprint,
        )
        # ① 先落案例（文档字段留空）
        case_row = await self._case_repo.create(draft, confirmed_by=actor.id)
        # ② 再建文档并回填（②失败时案例仍在，下次调用会走 _ensure_document 补上）
        case_row = await self._attach_document(actor, case_row, order, device, alert)
        return case_row, True

    async def get_case(self, actor: UserModel, case_id: int) -> DiagnosisCaseModel:
        case = await self._case_repo.find_by_id(case_id)
        if case is None:
            raise CaseError("CASE_NOT_FOUND", f"案例不存在：id={case_id}")
        self._ensure_visible(actor, case)
        return case

    async def find_by_work_order(
        self, actor: UserModel, work_order_id: int
    ) -> DiagnosisCaseModel | None:
        """这张工单沉淀的案例（没有则 None）——工单详情页据此显示「查看案例」。"""
        case = await self._case_repo.find_by_work_order(work_order_id)
        if case is None:
            return None
        self._ensure_visible(actor, case)
        return case

    async def list_cases(
        self,
        actor: UserModel,
        *,
        page: int,
        page_size: int,
        device_model: str | None = None,
        alert_code: str | None = None,
        work_order_id: int | None = None,
    ) -> tuple[list[DiagnosisCaseModel], int]:
        """分页列出可见案例：global 人人可见，tenant 案例仅本租户（engineer/admin 跨租户）。

        ``work_order_id`` 是给工单详情页用的——判断"这张工单沉淀了没有"。
        """
        if actor.role == "operator":
            if actor.tenant_id is None:
                raise CaseError("FORBIDDEN", "账号未绑定租户，无法查看案例")
            return await self._case_repo.list(
                page=page,
                page_size=page_size,
                tenant_id=actor.tenant_id,
                include_global=True,
                device_model=device_model,
                alert_code=alert_code,
                work_order_id=work_order_id,
            )
        return await self._case_repo.list(
            page=page,
            page_size=page_size,
            tenant_id=None,  # 不做租户限制
            device_model=device_model,
            alert_code=alert_code,
            work_order_id=work_order_id,
        )

    # ---------- 内部 ----------

    async def _ensure_document(
        self, actor: UserModel, case: DiagnosisCaseModel, order: WorkOrderModel
    ) -> DiagnosisCaseModel:
        """案例存在但文档缺失（上次崩在②）→ 补建。已有文档则原样返回。"""
        if case.document_id is not None:
            return case
        device = await self._device_repo.find_by_id(order.device_id)
        alert = await self._alert_repo.find_by_id(order.alert_id) if order.alert_id else None
        return await self._attach_document(actor, case, order, device, alert)

    async def _attach_document(
        self,
        actor: UserModel,
        case: DiagnosisCaseModel,
        order: WorkOrderModel,
        device: DeviceModel | None,
        alert: AlertModel | None,
    ) -> DiagnosisCaseModel:
        """生成案例的知识文档并回填到案例行上。"""
        form = DocumentUploadForm(
            scope=case.scope,
            tenant_id=case.tenant_id,  # global 时天然是 None，与 scope 一致
            doc_type=CASE_DOC_TYPE,
            doc_name=f"维修案例：{order.title}",
            version=None,
        )
        text = _render_case_markdown(case, order, device=device, alert=alert)
        document, index_task, _job = await self._document_service.create_from_text(
            actor,
            filename=f"diagnosis-case-{order.id}.md",  # 后缀必须是受支持的 .md
            text=text,
            form=form,
            source_type=CASE_SOURCE_TYPE,
            source_id=order.id,
        )
        updated = await self._case_repo.set_document(
            case.id, document_id=document.id, index_task_id=index_task.id
        )
        return updated or case

    async def _resolve_task_id(self, order: WorkOrderModel) -> int | None:
        """工单带了来源诊断报告时，顺着报告找出它属于哪次诊断任务。

        手工建的工单没有报告，这里就是 None——那是常态，不是异常。
        """
        if order.diagnosis_report_id is None:
            return None
        report = await self._report_repo.find_by_id(order.diagnosis_report_id)
        return report.task_id if report else None

    def _ensure_visible(self, actor: UserModel, case: DiagnosisCaseModel) -> None:
        if actor.role != "operator" or case.scope == "global":
            return
        if case.tenant_id != actor.tenant_id:
            raise CaseError("FORBIDDEN", "无权查看其他租户的案例")


# ---------- 模块级小工具 ----------


def _fingerprint(
    order: WorkOrderModel,
    device: DeviceModel | None,
    alert: AlertModel | None,
    root_cause: str,
    solution: str,
) -> str:
    """内容指纹：``机型|告警码|根因|方案`` 的 sha256。

    按**内容**而不是按工单算，是为了让"同一个故障被两张工单各自沉淀一次"命中同一个指纹，
    第二张工单会直接拿到已有案例，而不是在知识库里多堆一份重复文档。
    """
    parts = [
        _device_model_of(device),
        (alert.alert_code if alert else ""),
        _normalize(root_cause),
        _normalize(solution),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    """比较前把空白与大小写抹平——同一句话多打一个空格不该算成两条案例。"""
    return re.sub(r"\s+", "", (text or "").strip().lower())


def _device_model_of(device: DeviceModel | None) -> str:
    """案例的 ``device_model`` 是 NOT NULL，设备没填机型时用占位——不让沉淀整个失败。"""
    return (device.device_model if device and device.device_model else UNKNOWN_DEVICE_MODEL)


def _truncate(text: str | None, limit: int = MAX_SUMMARY_CHARS) -> str:
    value = (text or "").strip()
    return value if len(value) <= limit else f"{value[:limit]}…"


def _default_summary(order: WorkOrderModel, root_cause: str) -> str:
    return _truncate(f"{order.title}：{root_cause}")


def _derive_keywords(*texts: str) -> list[str]:
    """从标题/根因/方案里抽关键词（照 src1 的做法：ASCII 词整体取、中文取双字以上，去重保序）。

    抽得粗糙没关系——它的用途是给将来的检索/聚类一个抓手，不是精确分词。
    """
    joined = " ".join(text for text in texts if text).lower()
    terms = re.findall(r"[A-Za-z0-9_-]{3,}|[一-鿿]{2,}", joined)
    return list(dict.fromkeys(terms))[:MAX_KEYWORDS]


def _render_case_markdown(
    case: DiagnosisCaseModel,
    order: WorkOrderModel,
    *,
    device: DeviceModel | None,
    alert: AlertModel | None,
) -> str:
    """把案例渲染成一份 Markdown 知识文档——它同时是**给人看的案例**和**给检索用的片段源**。"""
    lines = [
        f"# 维修案例：{order.title}",
        "",
        "## 设备与故障",
        f"- 设备：{(device.device_code if device else '')} "
        f"{('（' + device.device_name + '）') if device and device.device_name else ''}".rstrip(),
        f"- 机型：{case.device_model}",
        f"- 告警码：{case.alert_code or '未关联告警'}",
        f"- 工单：#{order.id}",
    ]
    if alert is not None and alert.alert_name:
        lines.append(f"- 告警名称：{alert.alert_name}")
    if order.description:
        lines += ["", "## 故障现象", order.description]
    lines += [
        "",
        "## 确认根因",
        case.root_cause,
        "",
        "## 处理方案",
        case.solution,
        "",
        "## 处理结果",
        case.resolution_result,
    ]
    if case.keywords:
        lines += ["", "## 关键词", "、".join(case.keywords)]
    lines.append(f"\n> 沉淀于 {_iso(case.confirmed_at)}，来源工单 #{order.id}。")
    return "\n".join(lines) + "\n"


def _iso(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d") if value else "未知日期"
