from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

import pymupdf


# 页面上的一行文本
# y 取行顶部的纵坐标——段落恢复靠它算行间距；
# x0 目前只用于排查（单栏文档用不上，双栏才需要靠它分左右栏）。
@dataclass(frozen=True, slots=True)
class TextLine:

    page: int  # 页号，从 1 开始
    y: float
    x0: float
    text: str
    size: float  # 该行最大的字号


# 自己定义的，页面上的一块表格区域
@dataclass(frozen=True, slots=True)
class TableRegion:

    page: int
    y: float
    x0: float
    headers: list[str]
    rows: list[list[str]]
    bbox: tuple[float, float, float, float]

    @property
    def column_count(self) -> int:
        return len(self.headers)


@dataclass(frozen=True, slots=True)
class PageLayout:
    """整份 PDF 抽出来的元素。顺序上 lines 与 tables 各自按 (页, y) 递增。"""

    lines: list[TextLine] = field(default_factory=list)
    tables: list[TableRegion] = field(default_factory=list)
    # 页高（页号 → 页面高度）。判断"页眉页脚带"要用它，因为不同文档的页高不同。
    page_heights: dict[int, float] = field(default_factory=dict)
    # **目录页的行**：已从 lines 里剔除，但**暂存下来**——字号分不出层级时，
    # 后面要靠"目录反查"（目录里列着全文所有标题）来恢复章节结构。
    toc_lines: list[TextLine] = field(default_factory=list)
    # 页高用于判断一行是否位于页眉/页脚区域。不同 PDF 不一定都是 A4，不能写死 842pt。
    page_heights: dict[int, float] = field(default_factory=dict)


# 把 PDF 文件拆成 行 的文本和表格区域，
def analyze(data: bytes) -> PageLayout:
    document = pymupdf.open(stream=data, filetype="pdf")
    lines: list[TextLine] = []
    tables: list[TableRegion] = []
    page_heights: dict[int, float] = {}
    try:
        for page_number, page in enumerate(document, start=1):
            page_heights[page_number] = round(float(page.rect.height), 1)
            # 表格区域单独提取
            regions = _read_tables(page, page_number)
            tables.extend(regions)
            skip = [pymupdf.Rect(region.bbox) for region in regions]
            # 按行提取文字，跳过表格区域
            lines.extend(_read_lines(page, page_number, skip=skip))
    finally:
        document.close()
    return PageLayout(lines=lines, tables=tables, page_heights=page_heights)


def join_wrapped(text: str) -> str:
    """把 PDF 的折行拼回一行。

    PDF 里没有"段落"只有"行"，一句话被排版切断是常态。拼接规则按**字符类型**定：
    中英相邻补一个空格（``'告警码与处置'`` + ``'SOP》'`` → ``'告警码与处置 SOP》'``），
    同类型直接接（``'连杆机构、'`` + ``'分选挡板'`` → ``'连杆机构、分选挡板'``）。

    宁可多补一个空格也不要少补：分词会把多余空白吃掉，而 ``处置SOP`` 粘成一个 token
    会让 BM25 直接漏掉这一条。
    """
    parts = [line.strip() for line in text.splitlines() if line.strip()]
    joined = ""
    for part in parts:
        if joined and _needs_space(joined[-1], part[0]):
            joined += " "
        joined += part
    return joined


# ---------- 内部 ----------
# 按行读取文字，跳过表格区域
def _read_lines(
    page: pymupdf.Page, page_number: int, *, skip: list[pymupdf.Rect]
) -> list[TextLine]:
    lines: list[TextLine] = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:  # 1 = 图片，没有文本
            continue
        for line in block["lines"]:
            spans = line["spans"]
            text = "".join(span["text"] for span in spans).strip()
            if not text:
                continue  # PDF 里常有只含空白的"行"（标题下的空行就是这种）
            box = pymupdf.Rect(line["bbox"])
            if _inside_any(box, skip):
                continue  # 属于表格，交给表格那条路
            lines.append(
                TextLine(
                    page=page_number,
                    y=round(box.y0, 1),
                    x0=round(box.x0, 1),
                    text=text,
                    size=round(max(span["size"] for span in spans), 1),
                )
            )
    return lines


# 读取表格内容
def _read_tables(page: pymupdf.Page, page_number: int) -> list[TableRegion]:
    try:
        # page 是pymupdf中的数据对象，find_tables也是pymupdf的方法
        found = page.find_tables()
    except Exception:  # noqa: BLE001 —— 表格识别失败只降级，整份文档仍按纯文本走
        return []

    regions: list[TableRegion] = []
    for table in found.tables:
        # 将表头单独提取出来，后面的行放在一个总列表中，一行一个列表
        headers, rows = _read_grid(page, table)
        if not headers and not rows:
            continue
        regions.append(
            TableRegion(
                page=page_number,
                y=round(table.bbox[1], 1),
                x0=round(table.bbox[0], 1),
                headers=headers,
                rows=rows,
                bbox=tuple(round(value, 1) for value in table.bbox),
            )
        )
    return regions


# 按单元格 bbox 取原文，再切出表头与数据行。
def _read_grid(page: pymupdf.Page, table: object) -> tuple[list[str], list[list[str]]]:
    grid: list[list[str]] = []
    for row in table.rows:  # type: ignore[attr-defined]
        # 一行的单元格内容，单个单元格内用换行拼接给拼成了一行
        cells = [
            join_wrapped(page.get_text("text", clip=pymupdf.Rect(cell))) if cell else ""
            for cell in row.cells
        ]
        if any(cells):
            grid.append(cells)
    if not grid:
        return [], []
    # 把表格第一行的数据单独取出来，是表头
    headers = _pad(grid[0], max(len(row) for row in grid))
    # 计算表格多少列
    width = len(headers)
    # 列名缺失时补「列N」：单行成段要靠列名拼句子，没有列名那一行就废了
    headers = [cell or f"列{index + 1}" for index, cell in enumerate(headers)]
    # 返回表头以及后续每一行解包内容，都转成列表
    return headers, [_pad(row, width) for row in grid[1:]]


def _pad(row: list[str], width: int) -> list[str]:
    # 解包表行的内容，如果长度小于表格列数，后面补上几个空的
    return [*row, *([""] * (width - len(row)))] if len(row) < width else row[:width]


def _inside_any(box: pymupdf.Rect, regions: list[pymupdf.Rect]) -> bool:
    """行中心点落在表格矩形里就算属于表格。

    用中心点而不是"整个矩形被包含"：表格边界框有时会与紧邻的正文行重叠一两个点，
    按矩形包含判断会**误伤正文**。
    """
    point = pymupdf.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)
    return any(region.contains(point) for region in regions)


# 判断两行之间，是否是中英需要补一个空格
def _needs_space(left: str, right: str) -> bool:
    """中英相邻补一个空格；**但标点之后不补**。

    - ``'…告警码与处置'`` + ``'SOP》，…'`` → 补（原文本来就该有空格）
    - ``'…速度不匹配；'`` + ``'PLC 延时…'`` → 不补（中文标点后接英文是排版惯例，
      补了反而多一处空白）
    """
    if unicodedata.category(left).startswith("P"):
        return False
    return _is_cjk(left) != _is_cjk(right)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x2E80 <= code <= 0x9FFF  # 部首扩展 ~ 汉字
        or 0x3000 <= code <= 0x303F  # CJK 标点
        or 0xFF00 <= code <= 0xFFEF  # 全角字符
    )
