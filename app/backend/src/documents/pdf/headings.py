from __future__ import annotations

from collections.abc import Iterable

from app.backend.src.documents.pdf.layout import TextLine

# 一行的字符数少于这个值时，不拿它去投票选正文字号——标题、页码、页眉都短，
# 让它们参与统计会把正文字号带偏（实测一行标题 13 字，一行正文 45 字）。
VOTE_MIN_CHARS = 8

# 【标题字号的占比上限】—— 比正文大 ≠ 一定是标题。
# 实测 11 号手册：8.0 占 17%、9.0 占 13%，都比正文 7.0 大，但一个标题不可能占这么多字，
# 它们是表格/图注的另一套字号。标题的字符量永远很少（通常 <1%）。
MAX_TITLE_SHARE = 0.10

# 【相邻字号差小于这个值 → 视作同一层级】。
# 排版上 0.5~1pt 的差异常常只是"变体"（加粗版、表格版），不是新的一级。
# 实测老文档的断层是 21.9→17.1→14.6（差 4.8/2.5），而 11 号是 7.0→8.0→9.0（差 1.0），
# 后者挤成一团，应当合并成一档而不是拆成三级标题。
MIN_LEVEL_GAP = 1.5

# 对文本行内容，进行标题层级划分，后面转md文档要用
# 如果当前文件返回空字典，则表示该文档没有明显字号层级，应当降级处理
def classify(lines: Iterable[TextLine]) -> dict[float, int]:
    """字号 → 标题层级（1 起）。

    **返回空字典**表示"这份文档靠字号分不出层级"——调用方据此走下一步
    （目录反查 / 整行加粗 / 按页降级），而不是硬编一个层级出来。
    """
    # 得到正文的字号
    body = body_size(lines)
    if body is None:
        return {}

    # 每个字号占了多少字符（**不排除短行**：标题行本来就短，但它们是主角）
    weights: dict[float, int] = {}
    for line in lines:
        weights[line.size] = weights.get(line.size, 0) + len(line.text)
    total = sum(weights.values()) or 1

    # ① 候选：比正文大，**且字符量占比不高**（占比高的说明是"另一种正文"，不是标题）
    candidates = sorted(
        (
            size
            for size in weights
            if size > body and weights[size] / total <= MAX_TITLE_SHARE
        ),
        reverse=True,
    )
    if not candidates:
        return {}

    # ② 断层合并：相邻字号差太小就视作同一层（7.0/8.0/9.0 不该变成三级标题）
    groups: list[list[float]] = []
    for size in candidates:
        if groups and groups[-1][-1] - size < MIN_LEVEL_GAP:
            groups[-1].append(size)
        else:
            groups.append([size])

    # 字号越大层级越浅：{21.9: 1, 17.1: 2, 14.6: 3}
    return {
        size: level for level, group in enumerate(groups, start=1) for size in group
    }

# 将所有 不是短行 的正文文本，按字号计数，将字数最多的字号数定为正文。
# 如果所有 文本行 中的文本都少于8个字符
def body_size(lines: Iterable[TextLine]) -> float | None:
    weights: dict[float, int] = {}
    for line in lines:
        if len(line.text) >= VOTE_MIN_CHARS:
            # 哈希
            weights[line.size] = weights.get(line.size, 0) + len(line.text)
    if not weights:
        return None
    return max(weights, key=lambda size: weights[size])


def level_of(size: float, levels: dict[float, int]) -> int | None:
    """这一行的标题层级；不是标题则返回 ``None``。"""
    return levels.get(size)
