"""调用大模型前的个人信息脱敏（设计文档 §11.6）：手机号、身份证号、银行卡号、邮箱替换为占位符。

占位符按出现顺序编号；模型在回复里引用占位符时再换回原文（客户自己的信息）。
"""

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("邮箱", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("银行卡号", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
]


def mask(text: str, mapping: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
    """返回脱敏后的文本和"占位符 → 原文"的对应表（可传入已有的对应表继续编号）。"""
    mapping = {} if mapping is None else mapping
    reverse = {value: key for key, value in mapping.items()}
    for label, pattern in _PATTERNS:

        def replace(match: re.Match[str], label: str = label) -> str:
            value = match.group(0)
            if value in reverse:
                return reverse[value]
            count = sum(1 for key in mapping if key.startswith(f"[{label}"))
            placeholder = f"[{label}{count + 1}]"
            mapping[placeholder] = value
            reverse[value] = placeholder
            return placeholder

        text = pattern.sub(replace, text)
    return text, mapping


def unmask(text: str, mapping: dict[str, str]) -> str:
    for placeholder, value in mapping.items():
        text = text.replace(placeholder, value)
    return text


def detect(text: str) -> list[str]:
    """文本里出现的个人信息种类（邮箱、身份证号、银行卡号、手机号），按上面的顺序去重。"""
    found: list[str] = []
    for label, pattern in _PATTERNS:
        if pattern.search(text):
            found.append(label)
            text = pattern.sub(" ", text)
    return found


def strip_placeholders(text: str) -> str:
    """去掉脱敏占位符（写进档案的小结里不保留个人信息）。"""
    return re.sub(r"\[(?:邮箱|身份证号|银行卡号|手机号)\d+\]", "", text)
