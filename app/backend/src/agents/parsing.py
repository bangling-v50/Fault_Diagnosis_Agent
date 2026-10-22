from __future__ import annotations

import json
import re
from typing import Any

# 与数据库列长度对齐（见 models/diagnosis.py），留点余量
MAX_TITLE_CHARS = 200
MAX_CAUSE_CHARS = 500
MAX_ACTION_CHARS = 500
MAX_SUMMARY_CHARS = 2000
MAX_CLAIM_KEY_CHARS = 120

RISK_LEVELS = ("low", "medium", "high")
CONFIDENCE_LEVELS = ("high", "medium", "low")
PRIORITY_LEVELS = ("high", "medium", "low")
SHUTDOWN_VALUES = ("yes", "no", "conditional")


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型返回里抠出第一个 JSON 对象；抠不出来返回 None。

    三步走，从最严到最松：整段就是 JSON → 去掉 ``` 围栏后是 JSON → 贪婪匹配最外层的 ``{...}``
    （模型爱在 JSON 前后加"好的，这是分析结果："这类话）。贪婪匹配会取到**最后一个** ``}``，
    这对"只有一个 JSON 对象"的场景是对的；真要是被截断了，``json.loads`` 会失败，返回 None。
    """
    if not text:
        return None
    candidates = [text.strip(), _strip_code_fence(text).strip()]
    matched = re.search(r"\{.*\}", text, re.DOTALL)
    if matched:
        candidates.append(matched.group(0))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def normalize_report(raw: dict[str, Any]) -> dict[str, Any]:
    """把模型给的报告 JSON 规整成固定形状（不做事实层面的判断，那是硬校验的事）。"""
    return {
        "title": _as_text(raw.get("title"), limit=MAX_TITLE_CHARS) or "",
        "summary": _as_text(raw.get("summary"), limit=MAX_SUMMARY_CHARS) or "",
        "riskLevel": _as_enum(raw.get("riskLevel"), RISK_LEVELS),
        "possibleCauses": _normalize_items(
            raw.get("possibleCauses"),
            text_key="cause",
            level_key="confidence",
            levels=CONFIDENCE_LEVELS,
            limit=MAX_CAUSE_CHARS,
        ),
        "suggestedActions": _normalize_items(
            raw.get("suggestedActions"),
            text_key="action",
            level_key="priority",
            levels=PRIORITY_LEVELS,
            limit=MAX_ACTION_CHARS,
        ),
        "needShutdown": _as_enum(raw.get("needShutdown"), SHUTDOWN_VALUES),
        "needFieldService": _as_bool(raw.get("needFieldService")),
        "citations": _normalize_citations(raw.get("citations")),
    }


def normalize_verdict(raw: dict[str, Any]) -> dict[str, Any]:
    """把裁判模型的判定规整成 ``{verdict, score, issues}``。

    认不出 ``verdict`` 时返回 ``pass``——**裁判自己看不懂不算报告的错**，宁可放过一版，
    也不能因为解析歧义就无限重生成（那是最烧钱、最不产出的一种死法）。
    """
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in ("pass", "fail"):
        return {"verdict": "pass", "score": None, "issues": ["裁判返回无法解析，按通过处理"]}
    issues = [
        _as_text(item, limit=MAX_ACTION_CHARS) or ""
        for item in (raw.get("issues") or [])
        if isinstance(item, (str, int, float))
    ]
    return {
        "verdict": verdict,
        "score": _as_int(raw.get("score")),
        "issues": [item for item in issues if item],
    }


# ---------- 内部 ----------


def _strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 这类围栏。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def _as_text(value: Any, *, limit: int) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        value = str(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text if len(text) <= limit else f"{text[:limit]}…"


def _as_enum(value: Any, allowed: tuple[str, ...]) -> str | None:
    """枚举值大小写不敏感；不认识的返回 None（**不猜**，由硬校验去判定不合格）。"""
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if text in allowed else None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "是"):
            return True
        if lowered in ("false", "no", "否"):
            return False
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _normalize_items(
    value: Any,
    *,
    text_key: str,
    level_key: str,
    levels: tuple[str, ...],
    limit: int,
) -> list[dict[str, Any]]:
    """规整"原因/建议"这类列表。

    允许三种形态：``["文本", ...]``、``[{"cause": "...", ...}, ...]``、以及 dict 里只有文本。
    没有文本的条目直接丢弃（空条目没有任何信息量，留着只会污染报告和引用编号）。
    """
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in value:
        if isinstance(entry, str):
            text = _as_text(entry, limit=limit)
            if text:
                items.append({text_key: text, level_key: None, "evidenceIndexes": []})
            continue
        if not isinstance(entry, dict):
            continue
        raw_text = entry.get(text_key) or entry.get("text") or entry.get("content")
        text = _as_text(raw_text, limit=limit)
        if not text:
            continue
        items.append(
            {
                text_key: text,
                level_key: _as_enum(entry.get(level_key), levels),
                "evidenceIndexes": _as_index_list(
                    entry.get("evidenceIndexes") or entry.get("evidence")
                ),
            }
        )
    return items


def _as_index_list(value: Any) -> list[int]:
    """引用编号列表：允许单个数、数组、以及 "1,3" 这种字符串。

    **越界的编号原样保留**（不做 clamp），硬校验会把它揪出来——这正是"防编造引用"要抓的东西。
    """
    if value is None:
        return []
    if isinstance(value, (int, float, str)):
        value = [value]
    if not isinstance(value, list):
        return []
    indexes: list[int] = []
    for item in value:
        if isinstance(item, str):
            item = item.strip()
            if not item:
                continue
        number = _as_int(item)
        if number is not None and number not in indexes:
            indexes.append(number)
    return indexes


def _normalize_citations(value: Any) -> list[dict[str, Any]]:
    """规整引用边：``[{"evidenceIndex": 1, "claimKey": "possibleCause#1"}, ...]``。"""
    if not isinstance(value, list):
        return []
    citations: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        index = _as_int(entry.get("evidenceIndex") or entry.get("evidence_index"))
        if index is None:
            continue
        citations.append(
            {
                "evidenceIndex": index,
                "claimKey": _as_text(
                    entry.get("claimKey") or entry.get("claim_key"),
                    limit=MAX_CLAIM_KEY_CHARS,
                ),
            }
        )
    return citations
