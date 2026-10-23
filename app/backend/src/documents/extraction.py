from __future__ import annotations

from pathlib import Path

from app.backend.src.documents.errors import DocumentError
from app.backend.src.documents.pdf import parse

SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf"}


def suffix_of(filename: str) -> str:
    """取小写扩展名。

    **上传校验与抽取分派共用这一个函数**——两处各写一份 `{.md,.txt,.pdf}` 早晚会不一致。
    """
    return Path(filename or "").suffix.lower()


def is_supported(filename: str) -> bool:
    return suffix_of(filename) in SUPPORTED_EXTENSIONS


def extract_text(data: bytes, *, filename: str) -> str:
    suffix = suffix_of(filename)
    if suffix in (".md", ".txt"):
        return data.decode("utf-8", errors="replace")
    if suffix == ".pdf":
        # 目前只处理非扫描件版的文字表格pdf
        # 如果是扫描件，会得到一份只有页标题、没有正文的结果——这里不报错，交由上层
        # pymupdf 是懒导入的（和 openpyxl 一样）：它是 C 扩展，只有真处理 PDF 时才需要, 没必要让所有 import 本模块的路径都背上它。
        # 这里最后返回的是pdf渲染后的md文档 的文本内容
        return parse(data)
    raise DocumentError("UNSUPPORTED_FILE_TYPE", f"不支持的文件类型：{suffix or filename}")
