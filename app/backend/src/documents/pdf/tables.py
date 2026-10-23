from __future__ import annotations

from app.backend.src.documents.pdf.layout import TableRegion

# 单个单元格超过这个长度就截断——防止一格几万字的备注撑爆一个片段
MAX_CELL_CHARS = 200
# 单行拼出来的文本上限
MAX_ROW_CHARS = 800


# 将原本的一张表格渲染成 一片表格内容：一段表格说明+一行一段描述， 而且是这样 '表头="单元格值"'
def render(table: TableRegion) -> list[str]:
    if not table.rows:
        return []
    paragraphs = [_describe(table)]
    paragraphs.extend(_row_text(table, row) for row in table.rows)
    return [paragraph for paragraph in paragraphs if paragraph]


# 表格说明段行列结构 + 列名清单。
def _describe(table: TableRegion) -> str:
    columns = "、".join(table.headers)
    return f"表格：{table.column_count} 列 × {len(table.rows)} 行。列名依次为 {columns}。"


# 表格表头外，一行转一段内容
def _row_text(table: TableRegion, row: list[str]) -> str:
    pairs = [
        f'{header}="{_cell(value)}"'
        for header, value in zip(table.headers, row, strict=False)
        if value
    ]
    if not pairs:
        return ""  # 整行都是空单元格，丢掉
    text = "，".join(pairs)
    return text if len(text) <= MAX_ROW_CHARS else f"{text[:MAX_ROW_CHARS]}…"


def _cell(value: str) -> str:
    """压平空白、转义引号、必要时截断。"""
    text = " ".join(value.split())
    # 值里若带双引号，会与外面的引号错位（`列="他说"好的""`），换成单引号
    text = text.replace('"', "'")
    return text if len(text) <= MAX_CELL_CHARS else f"{text[:MAX_CELL_CHARS]}…"
