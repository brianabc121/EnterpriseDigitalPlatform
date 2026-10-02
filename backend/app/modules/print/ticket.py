"""小票的排版（设计文档 §29.5）。

80mm 热敏纸一行 48 列，汉字占 2 列、其他字符占 1 列。小票先排成和厂商无关的"行 + 样式"
（Line），对齐和分栏都靠按显示宽度补空格，所以同一张小票在两家打印机上长得一样，控制台里也能用
等宽字体预览；发送时再按厂商翻译成各家的标记（markup）。
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from unicodedata import east_asian_width
from zoneinfo import ZoneInfo

WIDTH = 48
# 放大一倍的字一行只有一半的列。
BIG_WIDTH = WIDTH // 2
# 明细最多打这么多行，再多就只打一句"详见控制台"（芯烨云的内容上限是 12K）。
MAX_ROWS = 80

Style = Literal["normal", "center", "big", "bold", "rule", "qr", "blank", "cut"]


@dataclass(frozen=True)
class Line:
    text: str = ""
    style: Style = "normal"

    def dump(self) -> dict[str, Any]:
        return {"text": self.text, "style": self.style}


def load(layout: list[dict[str, Any]]) -> list[Line]:
    return [Line(str(item.get("text", "")), item.get("style", "normal")) for item in layout]


def dump(lines: list[Line]) -> list[dict[str, Any]]:
    return [line.dump() for line in lines]


# ---- 显示宽度 ----


def char_width(ch: str) -> int:
    return 2 if east_asian_width(ch) in ("W", "F") else 1


def width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def truncate(text: str, cols: int) -> str:
    """截到 cols 列以内（不切半个汉字）。"""
    out, used = [], 0
    for ch in text:
        w = char_width(ch)
        if used + w > cols:
            break
        out.append(ch)
        used += w
    return "".join(out)


def pad(text: str, cols: int, align: str = "left") -> str:
    text = truncate(text, cols)
    fill = cols - width(text)
    if fill <= 0:
        return text
    if align == "right":
        return " " * fill + text
    if align == "center":
        left = fill // 2
        return " " * left + text + " " * (fill - left)
    return text + " " * fill


def wrap(text: str, cols: int) -> list[str]:
    """按显示宽度折行（在列的边界上切，汉字不切半个）。"""
    text = " ".join(text.split())
    if not text:
        return [""]
    rows: list[str] = []
    current: list[str] = []
    used = 0
    for ch in text:
        w = char_width(ch)
        if used + w > cols:
            rows.append("".join(current))
            current, used = [], 0
            if ch == " ":
                continue
        current.append(ch)
        used += w
    if current:
        rows.append("".join(current))
    return rows or [""]


def row(cells: list[tuple[str, int, str]], *, gap: int = 1) -> list[str]:
    """一行表格：cells 是 (文字, 列数, 对齐)。文字超过列数时在本列内折行，其他列补空。"""
    wrapped = [wrap(text, cols) for text, cols, _ in cells]
    height = max(len(w) for w in wrapped)
    lines = []
    for i in range(height):
        parts = [
            pad(wrapped[j][i] if i < len(wrapped[j]) else "", cols, align)
            for j, (_, cols, align) in enumerate(cells)
        ]
        lines.append((" " * gap).join(parts).rstrip())
    return lines


def plain(lines: list[Line]) -> str:
    """纯文本预览（控制台里用等宽字体显示；也是测试里比对的内容）。"""
    out = []
    for line in lines:
        if line.style == "rule":
            out.append("-" * WIDTH)
        elif line.style == "blank":
            out.append("")
        elif line.style == "cut":
            continue
        elif line.style == "qr":
            out.append(f"[二维码 {line.text}]")
        elif line.style in ("center", "big"):
            out.append(pad(line.text, WIDTH, "center").rstrip())
        else:
            out.append(line.text.rstrip())
    return "\n".join(out).rstrip("\n") + "\n"


# ---- 厂商标记 ----


def _escape(text: str) -> str:
    # 厂商用尖括号做标记，内容里的尖括号换成全角。
    return text.replace("<", "＜").replace(">", "＞")


def markup(lines: list[Line], brand: str) -> str:
    """翻译成厂商的排版标记。两家的标记不一样（<L> 在芯烨云是左对齐、在飞鹅云是字体变高）。"""
    out: list[str] = []
    for line in lines:
        text = _escape(line.text)
        if line.style == "rule":
            out.append("-" * WIDTH + "<BR>")
        elif line.style == "blank":
            out.append("<BR>")
        elif line.style == "cut":
            out.append("<CUT>")
        elif line.style == "qr":
            if brand == "xpyun":
                out.append(f"<QRCODE s=6 e=L l=center>{text}</QRCODE>")
            else:
                out.append(f"<QR>{text}</QR>")
        elif line.style == "center":
            # 芯烨云：换行标签要放在对齐标签里面；飞鹅云：放在外面。
            out.append(f"<C>{text}<BR></C>" if brand == "xpyun" else f"<C>{text}</C><BR>")
        elif line.style == "big":
            out.append(f"<CB>{text}<BR></CB>" if brand == "xpyun" else f"<CB>{text}</CB><BR>")
        elif line.style == "bold":
            out.append(f"<BOLD>{text}</BOLD><BR>")
        else:
            out.append(f"{text}<BR>")
    return "".join(out)


# ---- 小票 ----


def fmt_qty(value: Decimal | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    text = format(value.normalize(), "f")
    return text if text != "-0" else "0"


def fmt_time(value: datetime | None, tz: ZoneInfo, *, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return value.astimezone(tz).strftime(fmt) if value else "—"


class Builder:
    def __init__(self) -> None:
        self.lines: list[Line] = []

    def text(self, text: str) -> "Builder":
        self.lines.extend(Line(part) for part in wrap(text, WIDTH))
        return self

    def center(self, text: str) -> "Builder":
        self.lines.append(Line(truncate(text, WIDTH), "center"))
        return self

    def big(self, text: str) -> "Builder":
        self.lines.append(Line(truncate(text, BIG_WIDTH), "big"))
        return self

    def bold(self, text: str) -> "Builder":
        self.lines.append(Line(truncate(text, WIDTH), "bold"))
        return self

    def rule(self) -> "Builder":
        self.lines.append(Line("", "rule"))
        return self

    def blank(self) -> "Builder":
        self.lines.append(Line("", "blank"))
        return self

    def qr(self, text: str) -> "Builder":
        self.lines.append(Line(text, "qr"))
        return self

    def cut(self) -> "Builder":
        self.lines.append(Line("", "cut"))
        return self

    def field(self, label: str, value: str) -> "Builder":
        """ "标签：值"，值太长时折行、后续行缩进到值的位置。"""
        head = f"{label}："
        indent = width(head)
        parts = wrap(value, WIDTH - indent)
        self.lines.append(Line(head + parts[0]))
        self.lines.extend(Line(" " * indent + part) for part in parts[1:])
        return self

    def pair(self, left: str, right: str) -> "Builder":
        """一行里左右各一项。"""
        half = WIDTH // 2
        self.lines.append(Line((pad(left, half) + pad(right, half)).rstrip()))
        return self

    def table(self, cells: list[tuple[str, int, str]]) -> "Builder":
        self.lines.extend(Line(text) for text in row(cells))
        return self

    def footer(self, printed_by: str, seq: int, now: datetime, tz: ZoneInfo) -> "Builder":
        self.rule()
        self.pair(f"打印人：{printed_by}", f"第 {seq} 次打印")
        self.text(f"打印时间：{fmt_time(now, tz, fmt='%Y-%m-%d %H:%M:%S')}")
        return self


@dataclass(frozen=True)
class OrderLine:
    name: str
    model: str
    spec: str
    quantity: int
    unit: str = ""


@dataclass(frozen=True)
class MaterialLine:
    name: str
    spec: str
    unit: str
    planned: Decimal | None
    quantity: Decimal


def order_ticket(
    *,
    company: str,
    order_no: str,
    confirmed_at: datetime | None,
    customer: str,
    expected_at: datetime | None,
    worker: str,
    items: list[OrderLine],
    customer_note: str,
    internal_note: str,
    printed_by: str,
    seq: int,
    now: datetime,
    tz: ZoneInfo,
) -> list[Line]:
    """加工单：工人照着做的那张单子——不打价格。"""
    b = Builder()
    b.big(company).big("加工单").blank()
    b.bold(f"单号：{order_no}")
    b.field("确认时间", fmt_time(confirmed_at, tz))
    b.field("客户", customer or "—")
    b.field("期望时间", fmt_time(expected_at, tz))
    b.field("加工人", worker or "—")
    b.rule()
    # 序号 3 列、商品 36 列、数量 7 列，中间各空一格。
    b.table([("#", 3, "left"), ("商品", 36, "left"), ("数量", 7, "right")])
    for index, item in enumerate(items[:MAX_ROWS], start=1):
        detail = " / ".join(part for part in (item.model, item.spec) if part)
        qty = f"{item.quantity}{item.unit}" if item.unit else str(item.quantity)
        b.table([(str(index), 3, "left"), (item.name, 36, "left"), (qty, 7, "right")])
        if detail:
            b.table([("", 3, "left"), (f"型号/规格：{detail}", 44, "left")])
    if len(items) > MAX_ROWS:
        b.text(f"……共 {len(items)} 项，详见控制台")
    b.rule()
    b.text(f"共 {len(items)} 项，{sum(i.quantity for i in items)} 件")
    if customer_note:
        b.field("客户要求", customer_note)
    if internal_note:
        b.field("内部备注", internal_note)
    b.footer(printed_by, seq, now, tz)
    b.qr(order_no)
    b.cut()
    return b.lines


def requisition_ticket(
    *,
    company: str,
    document_no: str,
    order_no: str | None,
    customer: str | None,
    submitted_at: datetime | None,
    created_by: str,
    status_label: str,
    materials: list[MaterialLine],
    note: str,
    printed_by: str,
    seq: int,
    now: datetime,
    tz: ZoneInfo,
) -> list[Line]:
    """领料单：材料、建议数量和本次领用，留签字栏。"""
    b = Builder()
    b.big(company).big("领料单").blank()
    b.bold(f"单号：{document_no}")
    if order_no:
        b.field("关联订单", f"{order_no}（{customer}）" if customer else order_no)
    else:
        b.field("关联订单", "不关联订单")
    b.field("开单时间", fmt_time(submitted_at, tz))
    b.field("开单人", created_by or "—")
    b.field("状态", status_label)
    b.rule()
    # 材料 21、规格 9、单位 4、建议 5、领用 5，中间各空一格。
    header = [
        ("材料", 21, "left"),
        ("规格", 9, "left"),
        ("单位", 4, "left"),
        ("建议", 5, "right"),
        ("领用", 5, "right"),
    ]
    b.table(header)
    for material in materials[:MAX_ROWS]:
        b.table(
            [
                (material.name, 21, "left"),
                (material.spec, 9, "left"),
                (material.unit, 4, "left"),
                (fmt_qty(material.planned), 5, "right"),
                (fmt_qty(material.quantity), 5, "right"),
            ]
        )
    if len(materials) > MAX_ROWS:
        b.text(f"……共 {len(materials)} 项，详见控制台")
    b.rule()
    b.text(f"共 {len(materials)} 项")
    if note:
        b.field("备注", note)
    b.blank()
    b.pair("领料人签字：", "仓管签字：")
    b.blank()
    b.footer(printed_by, seq, now, tz)
    b.qr(document_no)
    b.cut()
    return b.lines


def test_ticket(
    *, company: str, printer_name: str, printed_by: str, now: datetime, tz: ZoneInfo
) -> list[Line]:
    b = Builder()
    b.big(company).big("打印测试").blank()
    b.field("打印机", printer_name)
    b.text("能看到这张小票，说明打印机已经接好。")
    b.text("一行 48 个字母、24 个汉字：")
    b.text("1234567890" * 4 + "12345678")
    b.text("汉字测试一二三四五六七八九十一二三四五六七八九十一二三四")
    b.footer(printed_by, 1, now, tz)
    b.cut()
    return b.lines
