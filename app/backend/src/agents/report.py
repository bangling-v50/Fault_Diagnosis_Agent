from __future__ import annotations

from typing import Any

RISK_LABELS = {"low": "低", "medium": "中", "high": "高"}
LEVEL_LABELS = {"low": "低", "medium": "中", "high": "高"}
SHUTDOWN_LABELS = {"yes": "需要停机", "no": "无需停机", "conditional": "视情况停机"}


def render_markdown(
    draft: dict[str, Any],
    *,
    evidence_by_no: dict[int, dict[str, Any]] | None = None,
    review_note: str | None = None,
) -> str:
    """渲染报告正文。

    ``evidence_by_no`` 用来把引用编号还原成"是哪条证据"（编号 → 证据条目），
    没传就只渲染编号。``review_note`` 是评审未通过时的提示语，会顶在正文最前面——
    一份没通过评审的报告如果一声不吭地发出去，比没有报告更糟。
    """
    lines: list[str] = []
    if review_note:
        lines.append(f"> ⚠️ {review_note}\n")

    title = str(draft.get("title") or "故障诊断报告").strip()
    lines.append(f"# {title}")

    facts = _join_parts(
        ("风险等级", RISK_LABELS.get(str(draft.get("riskLevel") or ""))),
        ("是否停机", SHUTDOWN_LABELS.get(str(draft.get("needShutdown") or ""))),
        ("现场服务", _field_service_text(draft.get("needFieldService"))),
    )
    if facts:
        lines.append(facts)

    summary = str(draft.get("summary") or "").strip()
    if summary:
        lines.append(f"## 情况说明\n{summary}")

    causes = draft.get("possibleCauses") or []
    if causes:
        body = "\n".join(
            f"{index}. {item.get('cause')}"
            f"{_level_suffix('置信度', item.get('confidence'))}"
            f"{_refs_suffix(item.get('evidenceIndexes'))}"
            for index, item in enumerate(causes, start=1)
        )
        lines.append(f"## 可能原因\n{body}")

    actions = draft.get("suggestedActions") or []
    if actions:
        body = "\n".join(
            f"{index}. {item.get('action')}"
            f"{_level_suffix('优先级', item.get('priority'))}"
            f"{_refs_suffix(item.get('evidenceIndexes'))}"
            for index, item in enumerate(actions, start=1)
        )
        lines.append(f"## 建议措施\n{body}")

    references = _references_block(draft, evidence_by_no or {})
    if references:
        lines.append(references)

    return "\n\n".join(lines)


# ---------- 内部 ----------


def _references_block(draft: dict[str, Any], evidence_by_no: dict[str, Any]) -> str:
    """引用依据：把报告里出现过的编号列出来，能还原到"哪条证据"就一并写上。"""
    numbers: list[int] = []
    for item in (draft.get("citations") or []):
        number = item.get("evidenceIndex")
        if isinstance(number, int) and number not in numbers:
            numbers.append(number)
    for group in ("possibleCauses", "suggestedActions"):
        for item in draft.get(group) or []:
            for number in item.get("evidenceIndexes") or []:
                if isinstance(number, int) and number not in numbers:
                    numbers.append(number)
    if not numbers:
        return ""
    rows = []
    for number in sorted(numbers):
        evidence = evidence_by_no.get(number)
        if evidence:
            label = evidence.get("kind_label") or evidence.get("kind") or "证据"
            title = evidence.get("source_title") or ""
            rows.append(f"- [{number}] {label}：{title}".rstrip("："))
        else:
            rows.append(f"- [{number}] （未在本次证据中找到，属无效引用）")
    return "## 引用依据\n" + "\n".join(rows)


def _level_suffix(label: str, value: Any) -> str:
    text = LEVEL_LABELS.get(str(value or ""))
    return f"（{label}：{text}）" if text else ""


def _refs_suffix(indexes: Any) -> str:
    if not isinstance(indexes, list) or not indexes:
        return ""
    return " 依据：" + "".join(f"[{number}]" for number in indexes)


def _field_service_text(value: Any) -> str | None:
    if value is True:
        return "需要现场服务"
    if value is False:
        return "无需现场服务"
    return None


def _join_parts(*pairs: tuple[str, str | None]) -> str:
    return " ｜ ".join(f"**{label}**：{value}" for label, value in pairs if value)
