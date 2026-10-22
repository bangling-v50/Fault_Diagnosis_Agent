from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.backend.src.agents.evaluator import evaluate_report as eval_report
from app.backend.src.agents.parsing import extract_json_object, normalize_report
from app.backend.src.agents.prompts import KIND_LABELS, build_report_prompt
from app.backend.src.agents.report import render_markdown
from app.backend.src.agents.state import DiagnosisState
from app.backend.src.background_jobs.runtime import BackgroundJobContext
from app.backend.src.llm_provider.chat import ChatError, ChatModelProvider
from app.backend.src.repositories.alerts import AlertRepository
from app.backend.src.repositories.diagnoses import (
    DiagnosisEvidenceRepository,
    DiagnosisReportRepository,
    DiagnosisStepRepository,
    EvidenceDraft,
    ReportEvidenceLinkRepository,
)
from app.backend.src.services.retrieval_service import RetrievalService
from app.backend.src.services.work_order_service import WorkOrderService

# 报告最多写几版（含第一版）。两版都被否就收下最后一版——诊断的价值在于给出东西。
MAX_REPORT_VERSIONS = 2
# 历史告警 / 历史工单各取最近几条进证据：够看出规律，又不至于把提示词撑爆
HISTORY_LIMIT = 5
# 进报告的知识库片段数（与在线检索的默认 top_k 一致）
KNOWLEDGE_TOP_K = 5


@dataclass(frozen=True, slots=True)
class DiagnosisDeps:
    """图需要的一切依赖（都是容器装配出来的单例）。

    **不含任何属于某次任务的东西**——那些走 ``state`` 与 ``context``。
    """

    chat_model: ChatModelProvider
    retrieval_service: RetrievalService
    alert_repo: AlertRepository
    work_order_service: WorkOrderService
    step_repo: DiagnosisStepRepository
    evidence_repo: DiagnosisEvidenceRepository
    report_repo: DiagnosisReportRepository
    link_repo: ReportEvidenceLinkRepository


def build_diagnosis_graph(deps: DiagnosisDeps, *, context: BackgroundJobContext) -> Any:
    """构建并编译本次任务专用的图。"""
    nodes = _DiagnosisNodes(deps, context)
    graph = StateGraph(DiagnosisState)
    graph.add_node("collect_knowledge", nodes.collect_knowledge)
    graph.add_node("collect_alert_history", nodes.collect_alert_history)
    graph.add_node("collect_work_orders", nodes.collect_work_orders)
    graph.add_node("write_report", nodes.write_report)
    graph.add_node("evaluate_report", nodes.evaluate_report)

    graph.add_edge(START, "collect_knowledge")
    graph.add_edge("collect_knowledge", "collect_alert_history")
    graph.add_edge("collect_alert_history", "collect_work_orders")
    graph.add_edge("collect_work_orders", "write_report")
    graph.add_edge("write_report", "evaluate_report")
    graph.add_conditional_edges(
        "evaluate_report",
        _route_after_evaluation,
        {"write_report": "write_report", "end": END},
    )
    return graph.compile()


def _route_after_evaluation(state: DiagnosisState) -> str:
    """评估后的唯一一条分支：判 fail 且还有额度 → 重写；否则收工。"""
    evaluation = state.get("evaluation") or {}
    version = int(state.get("report_version") or 0)
    if evaluation.get("verdict") == "fail" and version < MAX_REPORT_VERSIONS:
        return "write_report"
    return "end"


class _DiagnosisNodes:
    """节点集合。持有 deps 与**属于本次任务**的 context/state 视图，随图一起创建、用完即弃。"""

    def __init__(self, deps: DiagnosisDeps, context: BackgroundJobContext) -> None:
        self._deps = deps
        self._context = context

    # ---------- 工具①：知识库检索 ----------

    async def collect_knowledge(self, state: DiagnosisState) -> dict[str, Any]:
        """RAG 检索：拿告警码 + 告警名 + 机型去查手册/SOP/历史案例。"""
        await self._context.raise_if_cancelled()
        alert = state.get("alert") or {}
        device = state.get("device") or {}
        query = _build_query(alert, device)
        task_id = int(state["task_id"])

        try:
            result = await self._deps.retrieval_service.retrieve(
                query, tenant_id=int(state["tenant_id"]), top_k=KNOWLEDGE_TOP_K
            )
        except Exception as exc:  # noqa: BLE001 —— 检索失败只降级，不让整次诊断失败
            return await self._record_failure(
                task_id, "collect_knowledge", "知识库检索失败", exc, {"query": query}
            )

        drafts = [
            EvidenceDraft(
                kind="knowledge",
                source_type="knowledge_chunk",
                source_id=str(hit.chunk_id),
                source_title=f"文档 {hit.document_id} 第 {hit.chunk_index} 段",
                summary=hit.excerpt,
                payload={
                    "chunkId": hit.chunk_id,
                    "documentId": hit.document_id,
                    "chunkIndex": hit.chunk_index,
                    "vectorRank": hit.vector_rank,
                    "bm25Rank": hit.bm25_rank,
                    "rrfScore": hit.rrf_score,
                    "rerankScore": hit.rerank_score,
                },
            )
            for hit in result.hits
        ]
        step = await self._deps.step_repo.append(
            task_id=task_id,
            phase="collect_knowledge",
            status="SUCCEEDED",
            payload={
                "tool": "knowledge_retrieval",
                "query": query,
                "hitCount": len(drafts),
                "vectorCandidates": result.vector_candidates,
                "bm25Candidates": result.bm25_candidates,
                "rerankApplied": result.rerank_applied,
            },
        )
        entries = await self._persist_evidence(
            task_id, step.id, drafts, start_no=len(state.get("evidence_index") or [])
        )
        return {
            "evidence_index": entries,
            "collection_notes": (
                [] if drafts else ["知识库没有检索到与本次故障相关的片段"]
            ),
            "events": [_event("collect_knowledge", f"知识库检索到 {len(drafts)} 条片段")],
        }

    # ---------- 工具②：同设备同告警码的历史告警 ----------

    async def collect_alert_history(self, state: DiagnosisState) -> dict[str, Any]:
        alert = state.get("alert") or {}
        alert_code = str(alert.get("code") or "")
        task_id = int(state["task_id"])
        await self._context.raise_if_cancelled()

        try:
            rows = await self._deps.alert_repo.history_by_code(
                device_id=int(state["device_id"]), alert_code=alert_code, limit=HISTORY_LIMIT
            )
        except Exception as exc:  # noqa: BLE001
            return await self._record_failure(
                task_id, "collect_alert_history", "历史告警查询失败", exc, {"alertCode": alert_code}
            )

        # 本次告警自己已经在证据里了（kind=alert），再算进"历史"只是噪音——去掉它
        current_alert_id = alert.get("alertId")
        rows = [row for row in rows if row.id != current_alert_id][:HISTORY_LIMIT]
        drafts = [
            EvidenceDraft(
                kind="alert_history",
                source_type="alert",
                source_id=str(row.id),
                source_title=f"{row.alert_code} @ {_fmt_time(row.last_seen_at)}",
                summary=(
                    f"级别 {row.alert_level}，累计出现 {row.occurrence_count} 次，"
                    f"状态 {row.status}，首次 {_fmt_time(row.first_seen_at)}。"
                    f"原始报文：{row.raw_message or '无'}"
                ),
                payload={
                    "alertId": row.id,
                    "status": row.status,
                    "level": row.alert_level,
                    "occurrenceCount": row.occurrence_count,
                    "firstSeenAt": _iso(row.first_seen_at),
                    "lastSeenAt": _iso(row.last_seen_at),
                },
            )
            for row in rows
        ]
        step = await self._deps.step_repo.append(
            task_id=task_id,
            phase="collect_alert_history",
            status="SUCCEEDED",
            payload={"tool": "alert_history", "alertCode": alert_code, "count": len(drafts)},
        )
        entries = await self._persist_evidence(
            task_id, step.id, drafts, start_no=len(state.get("evidence_index") or [])
        )
        return {
            "evidence_index": entries,
            "collection_notes": (
                []
                if drafts
                else [f"这台设备此前没有出现过告警码 {alert_code} 的记录，属于首次发生"]
            ),
            "events": [
                _event("collect_alert_history", f"查到同故障历史告警 {len(drafts)} 条")
            ],
        }

    # ---------- 工具③：同设备同告警码的维修工单 ----------

    async def collect_work_orders(self, state: DiagnosisState) -> dict[str, Any]:
        alert = state.get("alert") or {}
        alert_code = str(alert.get("code") or "")
        task_id = int(state["task_id"])
        await self._context.raise_if_cancelled()

        try:
            items = await self._deps.work_order_service.history_by_code(
                device_id=int(state["device_id"]),
                alert_code=alert_code,
                limit=HISTORY_LIMIT,
                # 排掉"本次故障自己产生的那张工单"，它不是历史经验
                exclude_alert_id=alert.get("alertId"),
            )
        except Exception as exc:  # noqa: BLE001
            return await self._record_failure(
                task_id, "collect_work_orders", "历史工单查询失败", exc, {"alertCode": alert_code}
            )

        drafts = [
            EvidenceDraft(
                kind="work_order",
                source_type="work_order",
                source_id=str(item.work_order_id),
                source_title=f"工单 #{item.work_order_id} {item.title}（{item.status}）",
                summary=_work_order_summary(item),
                payload={
                    "workOrderId": item.work_order_id,
                    "status": item.status,
                    "priority": item.priority,
                    "createdAt": item.created_at,
                    "closedAt": item.closed_at,
                },
            )
            for item in items
        ]
        step = await self._deps.step_repo.append(
            task_id=task_id,
            phase="collect_work_orders",
            status="SUCCEEDED",
            payload={"tool": "work_order_history", "alertCode": alert_code, "count": len(drafts)},
        )
        entries = await self._persist_evidence(
            task_id, step.id, drafts, start_no=len(state.get("evidence_index") or [])
        )
        return {
            "evidence_index": entries,
            "collection_notes": (
                [] if drafts else [f"这台设备没有针对告警码 {alert_code} 的历史维修工单"]
            ),
            "events": [_event("collect_work_orders", f"查到历史维修工单 {len(drafts)} 条")],
        }

    # ---------- 写报告（回环里会跑第二次） ----------

    async def write_report(self, state: DiagnosisState) -> dict[str, Any]:
        await self._context.raise_if_cancelled()
        task_id = int(state["task_id"])
        evidence_index: list[dict[str, Any]] = list(state.get("evidence_index") or [])
        previous_issues = _previous_issues(state)

        # 构建提示词
        prompt = build_report_prompt(
            alert=state.get("alert") or {},
            device=state.get("device") or {},
            evidence_index=evidence_index,
            collection_notes=list(state.get("collection_notes") or []),
            previous_issues=previous_issues,
        )
        # 请求调用
        text = await self._deps.chat_model.ainvoke(prompt)
        # 提取文本，构造JSON
        raw = extract_json_object(text)
        if raw is None:
            # 见模块 docstring 第 3 条：写不出报告 = 模型没干活，上抛交给 job 退避重试
            raise ChatError(
                "报告生成失败：模型未返回可解析的 JSON"
                f"（原始返回前 200 字：{text[:200]}）"
            )
        # 构造结构化JSON报告草稿
        draft = normalize_report(raw)

        # ⚠️ 正文这里**不加**评审提示语。报告是"先写后评"，这一刻本版还没被评审，
        # 拿 previous_issues（**上一版**被否的原因）写进正文，会让一个【通过评审】的
        # 第二版也顶着"评审未通过"的警告。提示语改由评估节点在确定最终结果后补写，
        # 见 ``evaluate_report``。
        markdown = render_markdown(draft, evidence_by_no=_evidence_by_no(evidence_index))
        report = await self._deps.report_repo.create_version(
            task_id=task_id,
            title=draft.get("title") or None,  # 长度已在 normalize_report 里按列宽截断
            summary=str(draft.get("summary") or "") or None,
            risk_level=draft.get("riskLevel"),
            possible_causes=list(draft.get("possibleCauses") or []),
            suggested_actions=list(draft.get("suggestedActions") or []),
            need_shutdown=draft.get("needShutdown"),
            need_field_service=draft.get("needFieldService"),
            content=markdown,
            payload={
                "collectionNotes": list(state.get("collection_notes") or []),
                "previousIssues": previous_issues,
                "evidenceNoCount": len(evidence_index),
            },
            llm_model=self._deps.chat_model.model_name or None,
        )
        await self._link_report(report.id, draft, evidence_index)
        await self._deps.step_repo.append(
            task_id=task_id,
            phase="write_report",
            status="SUCCEEDED",
            payload={
                "version": report.version,
                "reportId": report.id,
                "llmModel": self._deps.chat_model.model_name,
                "previousIssues": previous_issues,
            },
        )
        return {
            "report_draft": draft,
            "report_id": report.id,
            "report_version": report.version,
            "events": [_event("write_report", f"已生成第 {report.version} 版诊断报告")],
        }

    # ---------- 评估 ----------

    async def evaluate_report(self, state: DiagnosisState) -> dict[str, Any]:
        await self._context.raise_if_cancelled()
        task_id = int(state["task_id"])
        evidence_index: list[dict[str, Any]] = list(state.get("evidence_index") or [])
        draft = state.get("report_draft") or {}
        version = int(state.get("report_version") or 0)

        # 评估报告情况
        evaluation = await eval_report(
            self._deps.chat_model,
            alert=state.get("alert") or {},
            device=state.get("device") or {},
            evidence_index=evidence_index,
            draft=draft,
            report_markdown=render_markdown(draft, evidence_by_no=_evidence_by_no(evidence_index)),
            collection_notes=list(state.get("collection_notes") or []),
        )
        exhausted = version >= MAX_REPORT_VERSIONS
        payload = {
            "version": version,
            "verdict": evaluation.verdict,
            "source": evaluation.source,
            "score": evaluation.score,
            "issues": evaluation.issues,
            "exhausted": exhausted,
        }
        await self._deps.step_repo.append(
            task_id=task_id,
            phase="evaluate_report",
            status="SUCCEEDED" if evaluation.passed else "FAILED",
            payload=payload,
        )
        report_id = state.get("report_id")
        if report_id:
            await self._deps.report_repo.set_review(int(report_id), review=payload)    # 每版都更新是否失败了
            # ⚠️ 到这一步才知道本版的**最终结果**。判 fail 且额度用尽 = 不再重写，
            # 这一版就是最终收下的，必须让正文自带"没通过评审"的标记——
            # 一份不合格的报告不能无声无息地流出去（可能被导出、发邮件、贴进工单）。
            #
            # 为什么必须在这里而不是 write_report：那时本版还没被评审，唯一能拿到的是
            # **上一版**的拒绝原因；而且写的那一刻无从知道本版会不会通过。
            if not evaluation.passed and exhausted:
                issues = [
                    *_previous_issues(state),  # 前几版为什么被否
                    *_versioned_issues(version, evaluation.issues),  # 本版自己的问题
                ]
                await self._deps.report_repo.update_content(
                    int(report_id),
                    content=render_markdown(
                        draft,
                        evidence_by_no=_evidence_by_no(evidence_index),
                        review_note=f"本报告未通过自动评审：{'；'.join(issues)}",
                    ),
                )
        update: dict[str, Any] = {
            "evaluation": payload,
            "events": [
                _event(
                    "evaluate_report",
                    f"第 {version} 版报告评审：{evaluation.verdict}"
                    + (f"（{'；'.join(evaluation.issues)}）" if evaluation.issues else ""),
                )
            ],
        }
        # 如果此次没通过，且次数没耗尽，则记录此次被拒原因。
        if not evaluation.passed and not exhausted:
            # 只有还要重写时才记 rejections——它会被喂进下一版提示词；
            # 已经超限收工的话，理由已经落在 report.payload 与 step 里了
            update["rejections"] = [
                {"version": version, "issues": evaluation.issues, "source": evaluation.source}
            ]
        return update

    # ---------- 内部 ----------
    # 记录失败
    async def _record_failure(
        self,
        task_id: int,
        phase: str,
        label: str,
        error: Exception,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        """工具失败时的统一收尾：不抛，记一步 FAILED + 一条采集说明，让流程继续走。"""
        reason = f"{label}：{type(error).__name__}: {error}"
        await self._deps.step_repo.append(
            task_id=task_id, phase=phase, status="FAILED", payload={**extra, "error": reason}
        )
        return {
            "collection_notes": [reason],
            "events": [_event(phase, reason)],
        }

    async def _persist_evidence(
        self,
        task_id: int,
        step_id: int,
        drafts: list[EvidenceDraft],
        *,
        start_no: int,
    ) -> list[dict[str, Any]]:
        """落盘一组证据，并返回给模型看的编号清单。

        ``no`` 是**跨三个工具的全局编号**（1..N），所以要从"已采条数"接着往下排——
        报告里的 evidenceIndexes 指的就是它，三个工具各从 1 开始的话引用会串位。
        """
        rows = await self._deps.evidence_repo.create_many(
            task_id=task_id, step_id=step_id, drafts=drafts
        )
        return [
            {
                "no": start_no + offset + 1,
                "evidence_id": row.id,
                "kind": row.kind,
                "kind_label": KIND_LABELS.get(row.kind, row.kind),
                "source_title": row.source_title,
                "summary": row.summary,
            }
            for offset, row in enumerate(rows)
        ]

    async def _link_report(
        self, report_id: int, draft: dict[str, Any], evidence_index: list[dict[str, Any]]
    ) -> None:
        """把报告里的引用编号翻译成证据 id，建成正式的引用边。"""
        by_no = _evidence_by_no(evidence_index)
        links: list[tuple[int, str | None]] = []
        for item in draft.get("citations") or []:
            entry = by_no.get(item.get("evidenceIndex"))
            if entry is not None:
                links.append((int(entry["evidence_id"]), item.get("claimKey")))
        if not links:
            return
        await self._deps.link_repo.link_many(report_id=report_id, links=links)


# ---------- 模块级小工具 ----------


def _build_query(alert: dict[str, Any], device: dict[str, Any]) -> str:
    """检索用的查询串：告警码 + 告警名 + 机型。

    刻意**不加**"故障""处理"这类泛词——它们在语料里到处都是，只会稀释检索信号
    （BM25 那一路尤其明显，两个字的泛词很容易在任意文档里撞上）。
    """
    parts = [alert.get("code"), alert.get("name"), device.get("deviceModel")]
    return " ".join(str(part).strip() for part in parts if part)


def _evidence_by_no(evidence_index: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """编号 → 证据条目。编号是证据自己带的 ``no``（跨工具全局），不是数组下标。"""
    return {int(item["no"]): item for item in evidence_index if "no" in item}


def _previous_issues(state: DiagnosisState) -> list[str]:
    """把历次被否的问题拍平——重写报告时原样喂回去。"""
    return _versioned_issues_from(state.get("rejections") or [])


def _versioned_issues(version: int, issues: list[str]) -> list[str]:
    """给某一版的问题打上版本号，格式与 :func:`_previous_issues` 保持一致。

    单独拎出来是因为"本版自己的问题"有两个用途：喂进下一版提示词（走 ``rejections``），
    以及超限收工时写进最终正文。两处格式必须一样，否则读报告的人会对不上号。
    """
    return [f"（第 {version} 版）{issue}" for issue in issues]


def _versioned_issues_from(rejections: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    for rejection in rejections:
        issues.extend(
            _versioned_issues(int(rejection.get("version") or 0), rejection.get("issues") or [])
        )
    return issues


def _work_order_summary(item: Any) -> str:
    parts = [f"状态 {item.status}"]
    if item.confirmed_root_cause:
        parts.append(f"确认根因：{item.confirmed_root_cause}")
    if item.solution:
        parts.append(f"处理方案：{item.solution}")
    if item.resolution_result:
        parts.append(f"处理结果：{item.resolution_result}")
    if not item.closed and not (item.confirmed_root_cause or item.solution):
        parts.append("该工单尚未结案，暂无维修结论")
    return "；".join(parts)


def _event(phase: str, message: str) -> dict[str, Any]:
    return {"step": phase, "message": message}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _fmt_time(value: Any) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value is not None else "时间未知"
