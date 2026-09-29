"""中文文本处理：关键词检索的词项、相似度与切片。

不依赖分词插件：中文按相邻两字（字的二元组）切分，英文和数字按整词，足以支撑 FAQ 这类短文本的
关键词召回；语义召回由向量模型负责（见 kb/search.py）。
"""

import re

_CJK_RUN = re.compile("[\\u3400-\\u4dbf\\u4e00-\\u9fff]+")
_WORD = re.compile(r"[a-z0-9]+")
# 寒暄和口头语几乎出现在所有问题里：切分前先去掉，免得和前后的字组成无意义的二元组。
_FILLERS = re.compile("你好|您好|请问|谢谢|麻烦|我想问|想问一下|问一下|一下")

CHUNK_SIZE = 500
CHUNK_OVERLAP = 60


def terms(text: str) -> list[str]:
    """词项：中文取相邻两字（只有一个字时取单字），英文和数字取整词，统一小写。"""
    lowered = _FILLERS.sub(" ", text.lower())
    found: set[str] = set()
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            found.add(run)
        else:
            found.update(run[i : i + 2] for i in range(len(run) - 1))
    found.update(_WORD.findall(lowered))
    return sorted(found)


def similarity(a: str, b: str) -> float:
    """两段文本词项的 Jaccard 相似度（0 到 1）。"""
    left, right = set(terms(a)), set(terms(b))
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def split_passages(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """把文档正文切成检索用的片段：先按空行分段，段落过长再按句号切，相邻片段重叠一小段。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= size:
            pieces.append(paragraph)
            continue
        sentences = [s for s in re.split(r"(?<=[。！？；.!?;])", paragraph) if s.strip()]
        current = ""
        for sentence in sentences:
            if current and len(current) + len(sentence) > size:
                pieces.append(current)
                current = current[-overlap:]
            current += sentence
            while len(current) > size:
                pieces.append(current[:size])
                current = current[size - overlap :]
        if current.strip():
            pieces.append(current)
    # 过短的相邻片段合并，减少检索噪声。
    merged: list[str] = []
    for piece in pieces:
        if merged and len(merged[-1]) + len(piece) + 1 <= size:
            merged[-1] = f"{merged[-1]}\n{piece}"
        else:
            merged.append(piece)
    return merged
