"""合同正文（设计文档 §34.2）：简单的 Markdown 和填写项 {{名称}}。

正文的写法（页面预览、导出 Word 和 AI 起草都按这一套）：

- `# 标题`（合同名称，居中）、`## 一级条款`、`### 二级条款`；
- 其余每一行是一个段落，空行只是间隔；`- ` 开头的是列表项；
- `|` 开头的连续几行是表格（第二行 `|---|---|` 是表头分隔，可以没有）；
- `**加粗**`；
- 填写项 `{{名称}}`：名称不超过 40 个字，不能有花括号和换行。
"""

import re
from dataclasses import dataclass, field

PLACEHOLDER = re.compile(r"\{\{\s*([^{}\n]{1,40}?)\s*\}\}")
MAX_FIELDS = 100
# 上传的模板里的空白：下划线、全角下划线、空着的括号；日期的"____年__月__日"算一个空白。
_DATE_BLANK = r"_{2,}\s*年\s*_{1,}\s*月\s*_{1,}\s*日"
_BLANK = re.compile(rf"{_DATE_BLANK}|_{{3,}}|＿{{2,}}|（[ 　]{{2,}}）|\([ 　]{{3,}}\)|【[ 　]*】")
# 空白前面的说明到这些符号为止（"甲方：____" 取"甲方"，"电话：{{电话}} 传真：____" 取"传真"）。
_LABEL_STOP = re.compile(r"[，。；;,!！？?\s]|\}\}")
_LABEL_TRIM = " \t　：:（(【[“\"'"
_MAX_LABEL = 20


def placeholders(body: str) -> list[str]:
    """正文里的填写项名称（按出现的先后，不重复）。"""
    names: list[str] = []
    for match in PLACEHOLDER.finditer(body):
        name = match.group(1).strip()
        if name and name not in names:
            names.append(name)
    return names


def fill(body: str, values: dict[str, str]) -> str:
    """把有值的填写项换成值；没有值的保留 {{名称}}。"""

    def replace(match: re.Match[str]) -> str:
        value = values.get(match.group(1).strip())
        return value if value else match.group(0)

    return PLACEHOLDER.sub(replace, body)


def missing(body: str, values: dict[str, str]) -> list[str]:
    """还没有值的填写项。"""
    return [name for name in placeholders(body) if not (values.get(name) or "").strip()]


def blank_out(body: str, values: dict[str, str]) -> str:
    """导出和打印时没填的填写项显示成"＿＿＿（名称）"。"""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        value = values.get(name)
        return value if value else f"＿＿＿（{name}）"

    return PLACEHOLDER.sub(replace, body)


def _label(before: str) -> str:
    """空白前面的说明：从这一行最后一个分隔符之后取，去掉冒号和括号。"""
    tail = _LABEL_STOP.split(before)[-1]
    return tail.strip(_LABEL_TRIM)[-_MAX_LABEL:].strip(_LABEL_TRIM)


def detect_blanks(text: str) -> tuple[str, list[str]]:
    """把上传的模板里的空白换成填写项，返回正文和新识别的填写项名称。

    名称取空白前面的文字（"甲方：____" → 甲方），取不到时叫"填写项 N"；同名的加上序号
    （"电话"、"电话 2"）。已经写成 {{名称}} 的保留。
    """
    names = placeholders(text)
    found: list[str] = []
    counter = 0
    lines: list[str] = []
    for line in text.split("\n"):
        out: list[str] = []
        last = 0
        for match in _BLANK.finditer(line):
            before = line[last : match.start()]
            out.append(before)
            label = _label("".join(out))
            if not label or PLACEHOLDER.search(label):
                counter += 1
                label = f"填写项 {counter}"
            name = label
            index = 2
            while name in names:
                name = f"{label} {index}"
                index += 1
            names.append(name)
            found.append(name)
            out.append("{{" + name + "}}")
            last = match.end()
            if len(names) >= MAX_FIELDS:
                break
        out.append(line[last:])
        lines.append("".join(out))
    return "\n".join(lines), found


# ---- 排版：标题、段落、列表、表格 ----


@dataclass(frozen=True)
class Run:
    text: str
    bold: bool = False


@dataclass
class Block:
    kind: str  # heading、paragraph、bullet、table
    level: int = 0  # 标题的级别（1–3）
    runs: list[Run] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)  # 表格
    header: bool = False  # 表格有表头


_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_SEPARATOR = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def runs(text: str) -> list[Run]:
    """一行文字按 **加粗** 拆成几段。"""
    result: list[Run] = []
    last = 0
    for match in _BOLD.finditer(text):
        if match.start() > last:
            result.append(Run(text[last : match.start()]))
        result.append(Run(match.group(1), bold=True))
        last = match.end()
    if last < len(text):
        result.append(Run(text[last:]))
    return result or [Run("")]


def _cells(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [cell.strip() for cell in inner.split("|")]


def blocks(body: str) -> list[Block]:
    """正文拆成标题、段落、列表项和表格。"""
    result: list[Block] = []
    lines = body.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        if line.lstrip().startswith("|"):
            rows: list[list[str]] = []
            header = False
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                current = lines[i].strip()
                if _SEPARATOR.match(current):
                    header = len(rows) == 1
                else:
                    rows.append(_cells(current))
                i += 1
            width = max(len(r) for r in rows) if rows else 0
            result.append(
                Block("table", rows=[r + [""] * (width - len(r)) for r in rows], header=header)
            )
            continue
        heading = _HEADING.match(line)
        if heading:
            result.append(
                Block("heading", level=len(heading.group(1)), runs=runs(heading.group(2)))
            )
        elif bullet := _BULLET.match(line):
            result.append(Block("bullet", runs=runs(bullet.group(1))))
        else:
            result.append(Block("paragraph", runs=runs(line.strip())))
        i += 1
    return result


def title_of(body: str) -> str:
    """正文的第一个一级标题（没有时为空）。"""
    for block in blocks(body):
        if block.kind == "heading" and block.level == 1:
            return "".join(r.text for r in block.runs).strip()
    return ""
