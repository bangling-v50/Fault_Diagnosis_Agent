import re
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.backend.src.documents.splitter import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE

# 表格说明段的格式（由 documents/pdf/tables.py 的 `_describe` 生成）。
# 靠它识别"这里开始是一张表" + 表有多少行。**格式改了这里要一起改。**
TABLE_HEAD_RE = re.compile(r"^表格：(\d+) 列 × (\d+) 行。列名依次为")
H2_RE = re.compile(r"^##\s+(.*)$")
H3_RE = re.compile(r"^###\s+(.*)$")
# 按二级标题切分（``^## `` 不会匹配 ``### ``：第三个字符是 # 而不是空格）
TOP_SECTION_RE = re.compile(r"(?m)^(?=## )")
# 段落分隔（允许空行里有空格）
PARAGRAPH_SEP_RE = re.compile(r"\n\s*\n")

# 子块类型
PARAGRAPH = "paragraph"
TABLE_HEAD = "table_head"
TABLE_ROW = "table_row"

# 「引出句」的长度上限：``每张误分单板应尽量保留：`` 12 字、``由此可形成归因规则：`` 10 字。
# 阈值给得宽松一点没关系——真正会误伤的只有"以冒号收尾的完整短句"，那种本来也该跟下文在一起。
LEAD_IN_MAX_CHARS = 40


@dataclass(frozen=True, slots=True)
class ChildChunk:
    """子块：参与召回的最小单元。"""

    index: int  # 在**本文档**里的全局序号（跨父块连续）
    content: str  # 标题路径 + 换行 + 正文
    element_type: str  # paragraph / table_head / table_row
    heading_path: str  # "## 1. 告警码总览 / ### 2.2 常见告警"


@dataclass(frozen=True, slots=True)
class ParentChunk:
    """父块：一个二级标题下的全部内容，只存不索引。"""

    index: int
    heading_path: str  # "## 1. 告警码总览"
    content: str  # 完整原文（含标题行与 Markdown 结构）
    children: list[ChildChunk]


# 返回父级块，但每个父级块内都有子块内容。父块列表（每个父块带自己的子块）
def chunk_document_hierarchy(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[ParentChunk]:
    normalized = text.strip()
    if not normalized:
        return []

    # 按 “## ”切分
    pieces = TOP_SECTION_RE.split(normalized)
    head, *sections = pieces

    parents: list[ParentChunk] = []
    # 第一个二级标题之前的内容（文档标题 + 引言）也要留住——README 里的"文档边界"
    # 那段就在这儿，丢了可惜
    if head.strip():
        parents.append(_build_parent(head, index=0, chunk_size=chunk_size, overlap=chunk_overlap))
    for piece in sections:
        parents.append(
            _build_parent(piece, index=len(parents), chunk_size=chunk_size, overlap=chunk_overlap)
        )
    return [parent for parent in parents if parent.children]


# 构造父块
def _build_parent(block: str, *, index: int, chunk_size: int, overlap: int) -> ParentChunk:
    block = block.strip()
    # 那这里应该是按段了吧？
    lines = block.splitlines()
    # 取父块的标题
    title = _top_title(lines)
    return ParentChunk(
        index=index,
        heading_path=f"## {title}",
        content=block,
        children=_children_of(block, parent_title=title, chunk_size=chunk_size, overlap=overlap),
    )


# 取出父块的标题，优先二级标题；第一个二级标题之前的那块，用文档标题（# …）
def _top_title(lines: list[str]) -> str:
    if lines:
        match = H2_RE.match(lines[0].strip())
        if match:
            return match.group(1).strip()
    for line in lines:
        if line.startswith("# ") and not line.startswith("## "):
            return line[2:].strip()
    return "全文"


# 把一个父块的正文切成子块
def _children_of(
    block: str, *, parent_title: str, chunk_size: int, overlap: int
) -> list[ChildChunk]:
    # 按段落划分
    paragraphs = [item.strip() for item in PARAGRAPH_SEP_RE.split(block) if item.strip()]
    children: list[ChildChunk] = []
    sub_title: str | None = None  # 当前所处的三级标题
    lead_in: str | None = None  # 攒着的引出句，等下一个单元一起落块
    cursor = 0

    while cursor < len(paragraphs):
        # 逐段逐段看
        paragraph = paragraphs[cursor]

        # 如果是三级标题，三级标题不单独成块——它进子块的标题路径，正文里不再重复
        heading3 = H3_RE.match(paragraph)
        if heading3:
            sub_title = heading3.group(1).strip()
            cursor += 1
            continue

        # 如果是父块自己的标题行，则略过，避免与标题头路径重复
        if paragraph.startswith("#"):
            # 父块自己的标题行（``## …``）自成一段——它已经进了 heading_path，
            # 再当正文切就会切出"路径 + 同一句标题"的重复子块
            cursor += 1
            continue

        # 引出句（"每张误分单板应尽量保留："）自己不成句，攒着跟下一个单元一起落块
        if lead_in is None and _is_lead_in(paragraph) and cursor + 1 < len(paragraphs):
            lead_in = paragraph
            cursor += 1
            continue

        path = _path(parent_title, sub_title)
        body = f"{lead_in}\n{paragraph}" if lead_in else paragraph
        lead_in = None

        # 判断是否到表格区域了，如果到了表格区域，连续几段都是表格，要连续处理
        table = TABLE_HEAD_RE.match(paragraph)
        if table:
            row_count = int(table.group(2))
            children.append(_child(children, path, body, TABLE_HEAD))
            for row in paragraphs[cursor + 1 : cursor + 1 + row_count]:
                children.append(_child(children, path, row, TABLE_ROW))
            cursor += 1 + row_count
            continue

        for piece in _split_long(body, chunk_size, overlap):
            children.append(_child(children, path, piece, PARAGRAPH))
        cursor += 1

    return children


def _is_lead_in(text: str) -> bool:
    """这一句是不是"引出句"。

    特征很直白：**很短**、**以「：」收尾**——它自己不构成一个可回答的内容，
    唯一职能是引出下面的列表 / 表格。让它单独成一个子块，就是让模型读到
    "每张误分单板应尽量保留：" 然后什么也没有。
    """
    stripped = text.rstrip()
    return len(stripped) <= LEAD_IN_MAX_CHARS and stripped.endswith("：")


def _child(children: list[ChildChunk], path: str, body: str, element_type: str) -> ChildChunk:
    return ChildChunk(
        index=len(children),
        content=f"{path}\n{body}",
        element_type=element_type,
        heading_path=path,
    )


def _path(parent_title: str, sub_title: str | None) -> str:
    """子块的标题路径，**只保留最近两级**（没有三级就只留二级）。"""
    if not sub_title:
        return f"## {parent_title}"
    return f"## {parent_title} / ### {sub_title}"


def _split_long(text: str, chunk_size: int, overlap: int) -> list[str]:
    """段落超长才切开；**切点尽量落在句子边界**，而不是硬切在第 N 个字。

    用 ``RecursiveCharacterTextSplitter`` 而不是裸切片：它会把 ``。！？；`` 这些
    当作优先分隔符，所以切出来的是完整的句子而不是半句话。
    """
    if len(text) <= chunk_size:
        return [text]
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap if overlap < chunk_size else 0,
        separators=["\n", "。", "！", "？", "；", "，", " ", ""],
    )
    return [piece.strip() for piece in splitter.split_text(text) if piece.strip()]
