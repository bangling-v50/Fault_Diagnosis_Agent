from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.backend.src.agents.parsing import extract_json_object, normalize_verdict
from app.backend.src.agents.prompts import build_evaluation_prompt
from app.backend.src.llm_provider.chat import ChatError, ChatModelProvider


@dataclass(frozen=True, slots=True)
class Evaluation:
    """评估结论。``source`` 说明这是哪一层给出的判断，回放时能分清"被硬校验拦下"和"被裁判否掉"。"""

    verdict: str  # "pass" | "fail"
    issues: list[str] = field(default_factory=list)
    score: int | None = None
    source: str = "hard"  # hard | llm | llm_unavailable

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


def check_report_hard(draft: dict[str, Any], *, evidence_count: int) -> list[str]:
    """硬校验：返回问题清单，空列表表示通过。

    最后一条是重点——**引用编号必须在本次证据的编号范围内**。模型编造引用（写个
    ``evidenceIndexes: [7]`` 而本次只有 3 条证据）是 RAG 幻觉里最典型、也最危险的一种：
    它让报告看起来"有据可查"，实际那条依据根本不存在。
    """
    issues: list[str] = []
    if not str(draft.get("title") or "").strip():
        issues.append("缺少报告标题")
    if not str(draft.get("summary") or "").strip():
        issues.append("缺少情况说明（summary）")
    if not draft.get("riskLevel"):
        issues.append("riskLevel 缺失或不是 low/medium/high 之一")
    if not draft.get("needShutdown"):
        issues.append("needShutdown 缺失或不是 yes/no/conditional 之一")
    if not draft.get("possibleCauses"):
        issues.append("possibleCauses 为空：报告必须至少给出一条可能原因")
    if not draft.get("suggestedActions"):
        issues.append("suggestedActions 为空：报告必须至少给出一条处理建议")

    out_of_range = sorted(
        {
            number
            for number in _referenced_indexes(draft)
            if not 1 <= number <= evidence_count
        }
    )
    if out_of_range:
        numbers = "、".join(str(number) for number in out_of_range)
        issues.append(
            f"引用了不存在的证据编号 {numbers}（本次证据范围是 1~{evidence_count}）——属于编造引用"
        )

    if evidence_count > 0 and not any(
        item.get("evidenceIndexes") for item in draft.get("possibleCauses") or []
    ):
        issues.append("本次采集到了证据，但所有可能原因都没有标注依据，无法判断结论从何而来")

    return issues


async def evaluate_report(
    model: ChatModelProvider,
    *,
    alert: dict[str, Any],
    device: dict[str, Any],
    evidence_index: list[dict[str, Any]],
    draft: dict[str, Any],
    report_markdown: str,
    collection_notes: list[str],
) -> Evaluation:
    """完整评估：硬校验过了才进软判。"""
    issues = check_report_hard(draft, evidence_count=len(evidence_index))
    if issues:
        return Evaluation(verdict="fail", issues=issues, source="hard")

    prompt = build_evaluation_prompt(
        alert=alert,
        device=device,
        evidence_index=evidence_index,
        report=draft,
        report_markdown=report_markdown,
        collection_notes=collection_notes,
    )
    try:
        text = await model.ainvoke(prompt)
    except ChatError as exc:
        return Evaluation(
            verdict="pass", issues=[f"评审模型不可用，未做软判：{exc}"], source="llm_unavailable"
        )

    raw = extract_json_object(text)
    if raw is None:
        return Evaluation(
            verdict="pass",
            issues=["评审结果不是可解析的 JSON，按通过处理"],
            source="llm_unavailable",
        )

    judged = normalize_verdict(raw)
    return Evaluation(
        verdict=judged["verdict"],
        issues=list(judged["issues"]),
        score=judged["score"],
        source="llm",
    )


def _referenced_indexes(draft: dict[str, Any]) -> list[int]:
    """报告里出现过的所有引用编号（明面的 citations + 藏在各条结论里的 evidenceIndexes）。"""
    numbers: list[int] = []
    for item in draft.get("citations") or []:
        number = item.get("evidenceIndex")
        if isinstance(number, int):
            numbers.append(number)
    for group in ("possibleCauses", "suggestedActions"):
        for item in draft.get(group) or []:
            numbers.extend(n for n in item.get("evidenceIndexes") or [] if isinstance(n, int))
    return numbers
