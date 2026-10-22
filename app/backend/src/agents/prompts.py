from __future__ import annotations

from typing import Any

# 单条证据摘要进提示词时的截断长度：证据可能有十几条，全量塞进去会把上下文撑满
MAX_EVIDENCE_CHARS = 600

# kind → 给模型看的中文来源名
KIND_LABELS = {
    "alert": "本次告警",
    "knowledge": "知识库片段",
    "alert_history": "历史告警",
    "work_order": "历史维修工单",
}


def build_report_prompt(
    *,
    alert: dict[str, Any],
    device: dict[str, Any],
    evidence_index: list[dict[str, Any]],
    collection_notes: list[str],
    previous_issues: list[str] | None = None,
) -> str:
    """写报告：输入告警快照 + 编号证据，输出固定 JSON。"""
    sections = [
        "你是工业设备售后运维的故障诊断专家。请**只依据下面给出的材料**写一份诊断报告。",
        _device_block(device),
        _alert_block(alert),
        _evidence_block(evidence_index),
    ]
    if collection_notes:
        sections.append(
            "## 证据采集过程中的问题\n"
            "（这些环节**没有拿到数据**，报告里必须如实体现，不许当成“没发现问题”）\n"
            + "\n".join(f"- {note}" for note in collection_notes)
        )
    if previous_issues:
        sections.append(
            "## 上一版报告被驳回的原因\n"
            "（这一版必须针对性修正，尤其是引用与证据对不上的问题）\n"
            + "\n".join(f"- {issue}" for issue in previous_issues)
        )

    sections.append(_report_contract(len(evidence_index)))
    return "\n\n".join(sections)


def build_evaluation_prompt(
    *,
    alert: dict[str, Any],
    device: dict[str, Any],
    evidence_index: list[dict[str, Any]],
    report: dict[str, Any],
    report_markdown: str,
    collection_notes: list[str],
) -> str:
    """当裁判：输入同样的证据 + 写好的报告，输出通过与否。"""
    sections = [
        "你是工业设备故障诊断报告的**评审员**。请判断下面这份报告是否合格。",
        _device_block(device),
        _alert_block(alert),
        _evidence_block(evidence_index),
    ]
    if collection_notes:
        sections.append(
            "## 证据采集过程中的问题\n" + "\n".join(f"- {note}" for note in collection_notes)
        )
    sections.append(f"## 待评审的报告\n{report_markdown}")
    sections.append(
        "## 评审标准\n"
        "逐条对照，只要有一条站不住就判 fail：\n"
        "1. **结论有据**：可能原因与建议措施都能对应到上面某条证据（引用编号必须在 1~"
        f"{len(evidence_index)} 之间），不是凭空猜测；\n"
        "2. **贴合现场**：结论针对的是这台设备的这个告警码，换台设备也成立的空话不算；\n"
        "3. **没有编造**：没有出现材料里不存在的设备型号、参数、历史记录；\n"
        "4. **自洽**：前后不矛盾（例如风险等级与建议的紧急程度不能打架）；\n"
        "5. **如实交代**：采集失败的环节有没有在报告里说明，而不是假装查到了。\n\n"
        "如果证据本身很少或采集失败，**不能因此判 fail**——合格的报告会如实写明"
        "“证据不足，无法确认根因”并给出保守建议。判 fail 的理由必须是报告本身站不住脚。"
    )
    sections.append(
        "## 输出格式\n"
        "**只输出一个 JSON 对象**，不要任何解释文字、不要 Markdown 代码围栏：\n"
        '{"verdict": "pass 或 fail", "score": 0 到 100 的整数, "issues": ["问题1", "问题2"]}\n'
        "- `verdict` 为 pass 时 `issues` 留空数组；为 fail 时每条问题都要具体到能照着重写"
        "（例如“第 2 条建议没有任何证据支撑”，而不是“结论不够好”）。"
    )
    return "\n\n".join(sections)


# ---------- 内部：几个材料区块 ----------


def _device_block(device: dict[str, Any]) -> str:
    lines = [
        f"- 设备编号：{device.get('deviceCode') or '未知'}",
        f"- 设备名称：{device.get('deviceName') or '未填'}",
        f"- 设备型号：{device.get('deviceModel') or '未填'}",
        f"- 安装位置：{device.get('installLocation') or '未填'}",
    ]
    return "## 设备信息\n" + "\n".join(lines)


def _alert_block(alert: dict[str, Any]) -> str:
    lines = [
        f"- 告警码：{alert.get('code') or '未知'}",
        f"- 告警名称：{alert.get('name') or '未填'}",
        f"- 告警级别：{alert.get('level') or '未填'}",
        f"- 原始报文：{alert.get('rawMessage') or '未提供'}",
        f"- 传感器摘要：{alert.get('sensorSummary') or '未提供'}",
        f"- 出现次数：{alert.get('occurrenceCount') or 1}",
        f"- 最近发生：{alert.get('lastSeenAt') or '未提供'}",
    ]
    return "## 本次告警\n" + "\n".join(lines)


def _evidence_block(evidence_index: list[dict[str, Any]]) -> str:
    """证据清单。**编号就是报告里该引用的编号**，所以顺序必须与落库时一致。"""
    if not evidence_index:
        return "## 可用证据\n（没有采集到任何证据——报告必须写明证据不足，不要猜测具体的根因）"
    blocks = ["## 可用证据\n（引用时只能使用这里的编号）"]
    for item in evidence_index:
        label = KIND_LABELS.get(str(item.get("kind")), str(item.get("kind")))
        title = item.get("source_title") or ""
        summary = _truncate(str(item.get("summary") or ""))
        blocks.append(f"[{item.get('no')}] {label}｜{title}\n{summary}")
    return "\n\n".join(blocks)


def _report_contract(evidence_count: int) -> str:
    return (
        "## 输出格式\n"
        "**只输出一个 JSON 对象**，不要任何解释文字、不要 Markdown 代码围栏：\n"
        "```\n"
        "{\n"
        '  "title": "一句话概括这次故障",\n'
        '  "summary": "3~5 句给现场看的情况说明：什么现象、最可能是什么问题、该怎么办",\n'
        '  "riskLevel": "low | medium | high",\n'
        '  "possibleCauses": [\n'
        '    {"cause": "可能原因", "confidence": "high | medium | low",'
        ' "evidenceIndexes": [1, 3]}\n'
        "  ],\n"
        '  "suggestedActions": [\n'
        '    {"action": "具体到能照着做的处理步骤", "priority": "high | medium | low",'
        ' "evidenceIndexes": [2]}\n'
        "  ],\n"
        '  "needShutdown": "yes | no | conditional",\n'
        '  "needFieldService": true,\n'
        '  "citations": [{"evidenceIndex": 1, "claimKey": "possibleCause#1"}]\n'
        "}\n"
        "```\n"
        f"- `evidenceIndexes` / `evidenceIndex` 只能取自上面证据清单的编号（1~{evidence_count}），"
        "**没有证据支撑就别填编号**，宁可留空数组——编造编号会被评审直接驳回；\n"
        "- `possibleCauses` 至少一条、`suggestedActions` 至少一条；\n"
        "- 证据不足时，在 `summary` 里明确写\"证据不足，无法确认根因\"，并给出保守的处理建议；\n"
        "- 全文使用中文，不要输出日期、不要编造型号与参数。"
    )


def _truncate(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MAX_EVIDENCE_CHARS else f"{text[:MAX_EVIDENCE_CHARS]}…"
