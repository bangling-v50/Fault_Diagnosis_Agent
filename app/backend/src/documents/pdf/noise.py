from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import replace

from app.backend.src.documents.pdf.layout import PageLayout, TextLine

MARGIN_POINTS = 72.0
MIN_REPEATED_MARGIN_PAGES = 3
MIN_TOC_LEADER_LINES = 3
MIN_TOC_NUMBERED_ENTRIES = 3
GENERIC_HEADER_PAGE_RATIO = 0.5

TOC_TITLES = frozenset({"目录", "目 录", "内容目录", "contents", "table of contents"})
TOC_LEADER_RE = re.compile(r"(?:[.．…�_\x08]\s*){4,}")
TOC_NUMBERED_ENTRY_RE = re.compile(r"^\s*\d+[.、]\s*\S+")
PAGE_NUMBER_RE = re.compile(
    r"^(?:"
    r"[-–—]\s*\d+\s*[-–—]"
    r"|第\s*\d+\s*页(?:\s*/\s*共\s*\d+\s*页)?"
    r"|\d+\s*/\s*\d+"
    r"|\d+"
    r")$",
    re.IGNORECASE,
)
TOC_RUNNING_HEADER_RE = re.compile(
    r"^(?:\d+\s+)?(?:目录|目\s*录|内容目录|contents|table of contents)(?:\s+\d+)?$",
    re.IGNORECASE,
)
DECORATIVE_LINE_RE = re.compile(r"^[\s_.·•\-–—=]+$")

# 图表清单：和目录同构（标题 + 页码），但标题名不同，要单独认出来
FIGURE_LIST_KEYWORDS = ("图表清单", "插图目录", "表格目录", "图目录", "表目录")

# 法律声明 / 版权页 / 修订历史：整页都是模板文字，没有检索价值
LEGAL_KEYWORDS = (
    "法律资讯",
    "版权",
    "免责",
    "保留所有权利",
    "copyright",
    "©",
    "修订历史",
    "版本历史",
    "更改记录",
)

# 封面：只认第一页，且行很少（09 的封面 2 行；12 的第一页 31 行——那不是封面）
COVER_MAX_LINES = 6

# 章节扉页上的大号编号：字号是全文最大，但只有短短几个字符（'1' / '2' / 'A'）。
# 实测 12 号手册的每章扉页就是这样——它是排版装饰，不是标题，检索时毫无用处。
# 判据用"全文最大字号 + 极短"，不需要知道正文字号，所以不受噪声清理顺序影响。
CHAPTER_NUMBER_MAX_CHARS = 3

# 前置内容（封面/声明/图表清单）**只在前若干页里找**。
# ⚠️ 实测教训：不加这个范围限制时，13 号手册有 21 页被误删——它**每页页脚都带版权字样**，
# "整页文本含'版权'"这条判据于是在正文页上命中，把正文整页删掉了。
#
# 范围按**页数的百分比**定（页数多前置内容也多），但加一个下限：
# 太小的文档按 10% 算只够 1~2 页，够不到图表清单（09 只有 18 页，清单在第 5 页）。
FRONT_MATTER_RATIO = 0.10
FRONT_MATTER_MIN_PAGES = 5
FRONT_MATTER_MAX_PAGES = 20


def _front_matter_limit(page_count: int) -> int:
    """前置内容最多查到第几页（含）。"""
    return min(
        FRONT_MATTER_MAX_PAGES,
        max(FRONT_MATTER_MIN_PAGES, int(page_count * FRONT_MATTER_RATIO)),
    )
LEADING_PAGE_RE = re.compile(r"^\s*\d+\s+(?=\D)")
TRAILING_PAGE_RE = re.compile(r"(?<=\D)\s+\d+\s*$")


def clean(layout: PageLayout) -> PageLayout:
    """返回去掉目录、封面、声明、图表清单、重复页眉页脚和独立页码后的布局。

    **目录不丢**：它的行被剔出 ``lines``，但**原样存进 ``toc_lines``**——
    后面恢复章节结构（字号分不出层级时）要靠它反查标题。
    """
    toc_pages = _toc_pages(layout.lines)
    toc_lines = [line for line in layout.lines if line.page in toc_pages]

    # 整页噪声：封面 / 法律声明 / 图表清单。目录页已经单独处理，这里跳过它。
    noise_pages = _noise_pages(layout.lines, skip=toc_pages)

    lines = [
        replace(line, text=_sanitize(line.text))
        for line in layout.lines
        if line.page not in toc_pages and line.page not in noise_pages
    ]
    lines = [line for line in lines if line.text and not DECORATIVE_LINE_RE.fullmatch(line.text)]
    lines = _drop_chapter_numbers(lines)
    margin_pages = _margin_pages(lines, layout.page_heights)
    content_page_count = max(1, len({line.page for line in lines}))
    repeated_margins = {
        signature
        for signature, pages in margin_pages.items()
        if len(pages) >= MIN_REPEATED_MARGIN_PAGES
    }
    kept_section_headers: set[tuple[str, str]] = set()
    cleaned_lines: list[TextLine] = []
    for line in lines:
        if _is_page_number(line, layout.page_heights):
            continue
        signature = _margin_signature(line, layout.page_heights)
        if signature not in repeated_margins:
            cleaned_lines.append(line)
            continue
        zone, _ = signature
        page_ratio = len(margin_pages[signature]) / content_page_count
        # 页脚没有结构价值，重复的全部删除。覆盖半数以上页面的页眉通常只是文档名，
        # 同样全部删除；章节性页眉则保留第一次作为结构标记，后续重复项删除。
        if zone == "bottom" or page_ratio >= GENERIC_HEADER_PAGE_RATIO:
            continue
        if signature not in kept_section_headers:
            kept_section_headers.add(signature)
            cleaned_lines.append(replace(line, text=_without_page_number(line.text)))
    cleaned_tables = [
        replace(
            table,
            headers=[_sanitize(value) for value in table.headers],
            rows=[[_sanitize(value) for value in row] for row in table.rows],
        )
        for table in layout.tables
        if table.page not in toc_pages and table.page not in noise_pages
    ]
    return PageLayout(
        lines=cleaned_lines,
        tables=cleaned_tables,
        page_heights=layout.page_heights,
        toc_lines=toc_lines,
    )


def _drop_chapter_numbers(lines: list[TextLine]) -> list[TextLine]:
    """丢掉章节扉页上的大号编号（'1' / '2' / 'A'）。

    判据：**字号是全文最大**，且**内容只有三个字符以内**。
    真实标题不会这么短——连"前言"都有两个字，而章号常常只有一个数字。
    """
    if not lines:
        return lines
    max_size = max(line.size for line in lines)
    return [
        line
        for line in lines
        if not (line.size == max_size and _looks_like_chapter_number(line.text))
    ]


def _looks_like_chapter_number(text: str) -> bool:
    """'1' / '12' / 'A' 这种**纯编号**才算章号。

    ⚠️ 不能只用"长度 ≤ 3"——实测把"前言"（2 个字）也误删了。必须是纯数字或单个字母。
    """
    stripped = text.strip()
    if not stripped or len(stripped) > CHAPTER_NUMBER_MAX_CHARS:
        return False
    return stripped.isdigit() or (len(stripped) == 1 and stripped.isalpha())


def _noise_pages(lines: list[TextLine], *, skip: set[int]) -> set[int]:
    """识别封面 / 法律声明 / 图表清单这类**整页**噪声。

    判据刻意保守：宁可漏删一页声明（多几段噪声），也不要错删前言或术语表（丢真内容）。
    """
    by_page: dict[int, list[TextLine]] = defaultdict(list)
    for line in lines:
        if line.page not in skip:
            by_page[line.page].append(line)

    result: set[int] = set()
    limit = _front_matter_limit(len(by_page))
    for index, (page, page_lines) in enumerate(sorted(by_page.items())):
        if index >= limit:
            break  # 前置内容不会出现在这么靠后的位置

        text = "\n".join(line.text for line in page_lines)

        # 图表清单：和目录长得一样，只是标题不同
        if any(keyword in text for keyword in FIGURE_LIST_KEYWORDS):
            result.add(page)
            continue

        # 法律声明 / 版权 / 修订历史
        lowered = text.casefold()
        if any(keyword.casefold() in lowered for keyword in LEGAL_KEYWORDS):
            result.add(page)
            continue

        # 封面：只认第一页，且行很少
        if index == 0 and len(page_lines) <= COVER_MAX_LINES:
            result.add(page)
    return result


def _toc_pages(lines: list[TextLine]) -> set[int]:
    """识别包含“目录”标题的连续点线页，连同末尾的目录空白续页一起删除。"""
    by_page: dict[int, list[str]] = defaultdict(list)
    for line in lines:
        by_page[line.page].append(line.text)

    info: dict[int, tuple[bool, int, int]] = {}
    for page, texts in by_page.items():
        normalized = [" ".join(text.split()).casefold() for text in texts]
        has_title = any(text in TOC_TITLES for text in normalized)
        leader_count = sum(bool(TOC_LEADER_RE.search(text)) for text in texts)
        numbered_entries = sum(bool(TOC_NUMBERED_ENTRY_RE.match(text)) for text in texts)
        info[page] = (has_title, leader_count, numbered_entries)

    candidates = sorted(
        page
        for page, (has_title, leader_count, _) in info.items()
        if has_title or leader_count >= MIN_TOC_LEADER_LINES
    )
    runs: list[list[int]] = []
    for page in candidates:
        if not runs or page != runs[-1][-1] + 1:
            runs.append([page])
        else:
            runs[-1].append(page)

    result: set[int] = set()
    for run in runs:
        has_title = any(info[page][0] for page in run)
        has_leaders = any(info[page][1] >= MIN_TOC_LEADER_LINES for page in run)
        has_numbered_entries = any(
            info[page][2] >= MIN_TOC_NUMBERED_ENTRIES for page in run
        )
        if has_title and (has_leaders or has_numbered_entries):
            result.update(run)
    return result


def _margin_pages(
    lines: list[TextLine], page_heights: dict[int, float]
) -> dict[tuple[str, str], set[int]]:
    pages_by_signature: dict[tuple[str, str], set[int]] = defaultdict(set)
    for line in lines:
        signature = _margin_signature(line, page_heights)
        if signature is not None:
            pages_by_signature[signature].add(line.page)
    return dict(pages_by_signature)


def _margin_signature(
    line: TextLine, page_heights: dict[int, float]
) -> tuple[str, str] | None:
    height = page_heights.get(line.page)
    if height is None:
        return None
    margin = min(MARGIN_POINTS, height * 0.12)
    if line.y <= margin:
        zone = "top"
    elif line.y >= height - margin:
        zone = "bottom"
    else:
        return None
    normalized = " ".join(line.text.split()).casefold()
    normalized = LEADING_PAGE_RE.sub("", normalized)
    normalized = TRAILING_PAGE_RE.sub("", normalized)
    return (zone, normalized) if normalized else None


def _is_page_number(line: TextLine, page_heights: dict[int, float]) -> bool:
    if _margin_signature(line, page_heights) is None:
        return False
    text = " ".join(line.text.split())
    return (
        PAGE_NUMBER_RE.fullmatch(text) is not None
        or TOC_RUNNING_HEADER_RE.fullmatch(text) is not None
    )


def _sanitize(text: str) -> str:
    """删除 PDF 字体映射产生的替换字符和不可见控制字符。"""
    return "".join(
        char
        for char in text
        if char != "�" and not unicodedata.category(char).startswith("C")
    ).strip()


def _without_page_number(text: str) -> str:
    text = LEADING_PAGE_RE.sub("", text)
    return TRAILING_PAGE_RE.sub("", text).strip()
