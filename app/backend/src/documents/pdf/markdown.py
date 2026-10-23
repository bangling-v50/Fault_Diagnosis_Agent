from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import groupby

from app.backend.src.documents.pdf import tables as table_renderer
from app.backend.src.documents.pdf.headings import classify
from app.backend.src.documents.pdf.layout import TableRegion, TextLine, analyze, join_wrapped
from app.backend.src.documents.pdf.noise import clean

# 段间距 = 行距基准的多少倍。实测段内/段间是 15 / 22（≈1.5 倍），取 1.3 留出余量：
# 宁可把"稍大的行距"判成新段落（多切一段无害），也不要让两段黏成一段。
PARAGRAPH_RATIO = 1.3
# 算行距基准时忽略过小与过大的间距：过小是同一行的不同 span，过大是跨栏/跨页
MIN_GAP = 1.0
MAX_GAP = 60.0
# 只有一行（算不出间距）时的兜底阈值
FALLBACK_GAP = 20.0
# Markdown 最多六级标题
MAX_HEADING_LEVEL = 6
# 列表项开头：有序（1. / 1、 / 1) ）与无序（- / • / · / *）
LIST_ITEM_RE = re.compile(r"^\s*(?:\d+[.、)]|[-•·*])\s*")
# 一句话的收尾标点。跨页时若上一页末行没有它，说明话没说完，该接到下一页去。
SENTENCE_END = ("。", "！", "？", "；", "：", "…", "」", "』", "”", "）", "】", "》")


@dataclass(frozen=True, slots=True)
class _Heading:
    page: int
    y: float
    level: int
    text: str


@dataclass(frozen=True, slots=True)
class _Paragraph:
    page: int
    y: float
    text: str

# 将 pdf 文件的字节数据，转成 md 文本
def parse(data: bytes) -> str:
    # 整份 PDF 抽出来的元素，顺序上 lines 与 tables 各自按 (页, y) 递增
    # 一个对象，内含两个元素，一个是装所有表格的表格区域对象列表，一个是装所有文本内容的（一行一个对象）文本行对象列表。
    # 内部按照页、纵坐标递增
    layout = clean(analyze(data))
    # 判断出正文字号，并给标题定好层级  {21.9: 1, 17.1: 2, 14.6: 3}
    levels = classify(layout.lines)
    if levels:
        # 如果文档有明显层级关系，则按标题层级构造父级块
        blocks = _structure(layout.lines, levels)
    else:
        # 如果文档没有层级关系，则降级为，按页构造父级块
        blocks = _structure_by_page(layout.lines)
    return _render(blocks, layout.tables)


# ---------- 组织成块 ----------
# 按字号切标题、按行间距切段落；**跨页时按规则判断要不要接续
def _structure(lines: list[TextLine], levels: dict[float, int]) -> list[object]:
    # 将 文本行 按照 页数 和 纵坐标 排序
    ordered = sorted(lines, key=lambda line: (line.page, line.y))
    # 正文行间距的阈值，大于就是段落
    thresholds = {page: _gap_threshold(page_lines) for page, page_lines in _by_page(lines)}

    blocks: list[object] = []
    pending: list[TextLine] = []
    previous: TextLine | None = None
    for line in ordered:
        # 如果前一行不空，而且 两页跨页之间不续、单页行间距过长 的情况，则把前面累积的文本行当作一段，组装
        if previous is not None and _breaks(previous, line, levels, thresholds):
            blocks.extend(_flush(pending))
            pending = []
        # 标题层级
        level = levels.get(line.size)
        # 如果当前行是标题行，则把之前累积的文本行作为一段，组装
        if level is not None:
            blocks.extend(_flush(pending))
            pending = []
            # 以 标题 身份加入
            blocks.append(_Heading(line.page, line.y, min(level, MAX_HEADING_LEVEL), line.text))
        # 如果都不是，则累积文本行
        else:
            pending.append(line)
        # 更新文本行游标
        previous = line
    blocks.extend(_flush(pending))
    return blocks


# 这一行要不要跟前面断开（即开一个新段落）
def _breaks(
    previous: TextLine, line: TextLine, levels: dict[float, int], thresholds: dict[int, float]
) -> bool:
    # 两行不在同一页，判断是否需要延续处理
    if line.page != previous.page:
        return not _cross_page_continues(previous, line, levels)
    # 页内：行间距超过这一页的阈值就是新段落
    return line.y - previous.y >= thresholds[line.page]


# 跨页延续处理
def _cross_page_continues(previous: TextLine, line: TextLine, levels: dict[float, int]) -> bool:
    # 如果是标题，则不续
    if levels.get(line.size) is not None:
        return False
    # 如果是 有序列表、无序列表 的内容，则续上
    if LIST_ITEM_RE.match(line.text):
        return True
    # 如果上句结尾不是句末标点，则续上
    return not previous.text.rstrip().endswith(SENTENCE_END)


# 降级：没有字号层级时，一页当作一个章节。
def _structure_by_page(lines: list[TextLine]) -> list[object]:
    blocks: list[object] = []
    for page_number, page_lines in _by_page(lines):
        blocks.append(_Heading(page_number, -1.0, 2, f"第 {page_number} 页"))
        threshold = _gap_threshold(page_lines)
        pending: list[TextLine] = []
        previous: TextLine | None = None
        for line in page_lines:
            if previous is not None and line.y - previous.y >= threshold:
                blocks.extend(_flush(pending))
                pending = []
            pending.append(line)
            previous = line
        blocks.extend(_flush(pending))
    return blocks

# 把累积的若干行拼成一个段落。
def _flush(lines: list[TextLine]) -> list[_Paragraph]:
    if not lines:
        return []
    text = join_wrapped("\n".join(line.text for line in lines))
    if not text:
        return []
    return [_Paragraph(lines[0].page, lines[0].y, text)]


# 按页分组，页内按 y 从上到下
def _by_page(lines: list[TextLine]) -> list[tuple[int, list[TextLine]]]:
    ordered = sorted(lines, key=lambda line: (line.page, line.y))
    # [(页号，[当前页所有文本行])……]
    return [(page, list(group)) for page, group in groupby(ordered, key=lambda line: line.page)]


# 返回当前这一页“新段落”的行间距阈值，通过两行之间的上边界纵坐标之间的距离来算的行间距
def _gap_threshold(page_lines: list[TextLine]) -> float:
    gaps = sorted(
        later.y - earlier.y
        for earlier, later in zip(page_lines, page_lines[1:], strict=False)
        if MIN_GAP < later.y - earlier.y < MAX_GAP
    )
    if not gaps:
        return FALLBACK_GAP  # 整页只有一行
    # 10% 分位 ≈ 行距（段内换行是最小最密集的一档）
    base = gaps[len(gaps) // 10]
    return max(base * PARAGRAPH_RATIO, MIN_GAP * 2)


# ---------- 渲染 ----------
# 把文本块blocks和表格，按照页码和纵坐标，排回文档顺序，渲染成md文档（表格从提取时，一直都是单独存放的，文本行的分段、换行接续都不影响）
def _render(blocks: list[object], regions: list[TableRegion]) -> str:
    items: list[tuple[int, float, int, object]] = [
        (block.page, block.y, 0, block) for block in blocks
    ]
    items.extend((region.page, region.y, 1, region) for region in regions)
    # 把文本行组装的段落和标题内容，和表格混在一起，按照页码、上边界纵坐标的顺序排序，若文字和表格完全冲突，先文字后表格。
    items.sort(key=lambda item: (item[0], item[1], item[2]))

    pieces: list[str] = []
    for _, _, kind, payload in items:
        # 如果是表格，则使用专门的表格渲染器
        if kind == 1:
            pieces.extend(table_renderer.render(payload))  # type: ignore[arg-type]
            continue
        # 如果是标题，则按照标题层级添加 # 数量
        if isinstance(payload, _Heading):
            pieces.append(f"{'#' * payload.level} {payload.text}")
        # 正文就普通添加就行
        else:
            pieces.append(payload.text)  # type: ignore[union-attr]
    return "\n\n".join(pieces).strip()
