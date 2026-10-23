from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

DEFAULT_CHUNK_SIZE = 1200
DEFAULT_CHUNK_OVERLAP = 200


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    """切分产物：一段文本 + 它在原文中的位置。

    ``start`` / ``end`` 是**在抽取出的全文里的字符偏移**（不是文件字节偏移），
    界面上高亮命中原文要靠它；``heading_path`` 只在 markdown 策略下有值（如 "第 3 章 / 安全须知"）。
    """

    index: int
    content: str
    start: int
    end: int
    heading_path: str | None = None

    @property
    def metadata(self) -> dict[str, int | str]:
        """位置元数据。

        ⚠️ 目前**尚未**写入 Milvus——chunk schema 里没有 ``metadata`` 字段。
        等确定"要不要保留位置信息"后，再决定是加字段还是删掉这个属性。
        """
        metadata: dict[str, int | str] = {
            "chunkIndex": self.index,
            "start": self.start,
            "end": self.end,
        }
        if self.heading_path is not None:
            metadata["headingPath"] = self.heading_path
        return metadata


# 文档切分入口
def chunk_document_text(
        text: str,
        *,
        strategy: str = "fixed-character",
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
) -> list[DocumentChunk]:
    # 合格性校验
    effective_chunk_size = DEFAULT_CHUNK_SIZE if chunk_size is None else chunk_size
    effective_chunk_overlap = chunk_overlap if chunk_overlap is not None else DEFAULT_CHUNK_OVERLAP
    if effective_chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    if effective_chunk_overlap < 0:
        raise ValueError("chunk_overlap must be zero or greater")

    normalized = text.strip()
    if not normalized:
        return []
    # md文档标题
    if strategy == "markdown-heading":
        return _heading_chunks(normalized, effective_chunk_size)
    # 按段落
    if strategy == "paragraph":
        return _paragraph_chunks(normalized, effective_chunk_size)
    # 除此之外，如果还不是固定字符，就报错
    if strategy != "fixed-character":
        raise ValueError("Unsupported chunking strategy")
    # 重叠步长比size还大，报错
    if effective_chunk_overlap >= effective_chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")
    # 固定长度字符
    return _fixed_chunks(normalized, effective_chunk_size, effective_chunk_overlap)


# 按固定长度字符切分
def _fixed_chunks(text: str, chunk_size: int, chunk_overlap: int) -> list[DocumentChunk]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        # 中文没有空格分词：若不把中文标点作为分隔符，会一路落到 ""（逐字符切），
        # 片段就会从句子中间断开。加上标点，让片段尽量落在句子/分句边界。
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
    )
    return _chunks_from_contents(
        splitter.split_text(text),
        text,
        chunk_overlap=chunk_overlap,
    )


# 按段落切分
def _paragraph_chunks(text: str, chunk_size: int) -> list[DocumentChunk]:
    paragraphs = [item.strip() for item in text.split("\n\n") if item.strip()]  # 按空行分段落
    return _group_units(paragraphs, text, chunk_size, None)  # 多个段落合并到 chunk_size


# 按markdown文档标题切分
def _heading_chunks(text: str, chunk_size: int) -> list[DocumentChunk]:
    splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[
            ("#", "h1"),
            ("##", "h2"),
            ("###", "h3"),
            ("####", "h4"),
            ("#####", "h5"),
            ("######", "h6"),
        ],
        strip_headers=False,
    )
    # 每个标题下的内容 = 一个 chunk（保留标题路径 heading_path）
    documents = splitter.split_text(text)
    # 最终切分结果
    chunks: list[DocumentChunk] = []
    cursor = 0
    for document in documents:
        content = document.page_content.strip()
        if not content:
            continue
        heading_path = _heading_path(document.metadata)
        start = text.find(content, cursor)
        start = start if start >= 0 else text.find(content)
        start = max(start, 0)
        # 某标题内容超长 → 再用 fixed 切
        if len(content) > chunk_size:
            for chunk in _fixed_chunks(content, chunk_size, 0):
                chunks.append(
                    DocumentChunk(
                        len(chunks),
                        chunk.content,
                        start + chunk.start,
                        start + chunk.end,
                        heading_path,
                    )
                )
        else:
            chunks.append(
                DocumentChunk(
                    len(chunks),
                    content,
                    start,
                    start + len(content),
                    heading_path,
                )
            )
        cursor = start + len(content)
    if chunks:
        return chunks
    return _group_units([text], text, chunk_size, "heading")


# 按固定字符切分
def _chunks_from_contents(
        contents: Sequence[str],
        original: str,
        *,
        chunk_overlap: int,
) -> list[DocumentChunk]:
    chunks: list[DocumentChunk] = []
    cursor = 0
    for raw_content in contents:
        content = raw_content.strip()
        if not content:
            continue
        start = original.find(content, cursor)
        if start < 0:
            start = original.find(content)
        start = max(start, 0)
        chunks.append(DocumentChunk(len(chunks), content, start, start + len(content)))
        cursor = max(start + 1, start + len(content) - chunk_overlap)
    return chunks


def _heading_path(metadata: Mapping[str, object]) -> str | None:
    parts = [
        value
        for key in ("h1", "h2", "h3", "h4", "h5", "h6")
        if isinstance((value := metadata.get(key)), str)
    ]
    if not parts:
        return None
    return " / ".join(parts)


def _emit(
        chunks: list[DocumentChunk],
        content: str,
        *,
        original: str,
        cursor: int,
        chunk_size: int,
        kind: str | None,
) -> int:
    """把 ``content`` 落成一个（或几个）片段，返回推进后的 cursor。

    **这是 ``_group_units`` 里唯一的落盘出口**——超长兜底跟着落盘走，而不是单独挂在
    循环外面。之前那种写法只在"循环结束时残留的 ``current``"上检查超长，于是
    **排在中间的超长段落会原样落成一个巨大片段**（实测：600 字段落、``chunk_size=200``，
    放最前/中间都不切，只有放最后才切）。

    超长时用 :func:`_fixed_chunks` 定长切开，并把切片的相对偏移换算成**原文偏移**
    （``start + piece.start``）——位置信息是前端高亮命中原文的依据，不能丢。
    """
    start = max(original.find(content, cursor), 0)
    heading = content.splitlines()[0] if kind else None
    if len(content) > chunk_size:
        for piece in _fixed_chunks(content, chunk_size, 0):
            chunks.append(
                DocumentChunk(
                    len(chunks),
                    piece.content,
                    start + max(piece.start, 0),
                    start + max(piece.end, 0),
                    heading,
                )
            )
    else:
        chunks.append(DocumentChunk(len(chunks), content, start, start + len(content), heading))
    return start + len(content)


def _group_units(
        units: list[str], original: str, chunk_size: int, kind: str | None
) -> list[DocumentChunk]:
    """把若干"单元"（段落 / 标题块）贪心合并到 ``chunk_size``。

    ``chunk_size`` 在这里是**软上限**：合并出来的片段尽量不超过它，但**单个单元自己就超长**时
    只能切开（交给 :func:`_emit`）——所以最终片段要么 `<= chunk_size`，要么是"一个超长单元
    被定长切分后的一小段"，不会出现整段原样落盘的巨块。
    """
    chunks: list[DocumentChunk] = []
    current = ""
    cursor = 0
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if current and len(candidate) > chunk_size:
            # 装不下了：先把 current 落掉（超长就切开），再从 unit 重新累积
            cursor = _emit(
                chunks,
                current,
                original=original,
                cursor=cursor,
                chunk_size=chunk_size,
                kind=kind,
            )
            current = unit
        else:
            current = candidate
    if current:
        _emit(
            chunks,
            current,
            original=original,
            cursor=cursor,
            chunk_size=chunk_size,
            kind=kind,
        )
    return chunks
