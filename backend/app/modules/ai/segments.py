"""较长的 AI 回答分段发送（设计文档 §3.3：OpenIM 开源版没有稳定的流式消息，用"正在输入"提示 +
分段发送）。只用于网页 Widget；微信客服每条消息都占 48 小时内 5 条的额度，不分段。"""

import re

# 超过这么多字才分段；每段尽量不超过 SEGMENT_MAX 字，按段落和句子切，最多 MAX_SEGMENTS 段。
SEGMENT_FROM = 120
SEGMENT_MAX = 150
MAX_SEGMENTS = 4
_SENTENCE = re.compile(r"(?<=[。！？；!?;])")


def split_reply(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= SEGMENT_FROM:
        return [text]
    pieces: list[str] = []
    for paragraph in (p.strip() for p in text.splitlines()):
        if not paragraph:
            continue
        if len(paragraph) <= SEGMENT_MAX:
            pieces.append(paragraph)
            continue
        current = ""
        for sentence in (s for s in _SENTENCE.split(paragraph) if s.strip()):
            if current and len(current) + len(sentence) > SEGMENT_MAX:
                pieces.append(current)
                current = ""
            current += sentence
        if current:
            pieces.append(current)
    merged: list[str] = []
    for piece in pieces:
        if merged and len(merged[-1]) + len(piece) + 1 <= SEGMENT_MAX:
            merged[-1] = f"{merged[-1]}\n{piece}"
        else:
            merged.append(piece)
    if len(merged) > MAX_SEGMENTS:
        merged = [*merged[: MAX_SEGMENTS - 1], "\n".join(merged[MAX_SEGMENTS - 1 :])]
    return merged or [text]
