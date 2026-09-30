"""商品库的 Excel 模板与逐行校验（设计文档 §25.2）。

模板第一行是表头，第二行是示例；每一列选中时显示填写说明，价格列只能填不小于 0 的数字；
另有"填写说明"工作表。上传的表格按表头名称识别列（顺序不限，"名称*""名称（必填）"都可以），
示例行原样保留时自动跳过。
"""

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.core.xlsx import Cell, Column, Sheet, write_workbook
from app.modules.kb.parsers import ParseError, parse_sheet, suffix
from app.modules.products.service import normalize_category, split_aliases

MAX_ROWS = 5000
MAX_BYTES = 10 * 1024 * 1024
MAX_PRICE = Decimal("9999999999.99")
CENT = Decimal("0.01")
URL = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


@dataclass(frozen=True)
class SheetColumn:
    key: str
    title: str
    required: bool
    hint: str
    width: int = 14
    money: bool = False
    limit: int = 0
    aliases: tuple[str, ...] = ()


COLUMNS: tuple[SheetColumn, ...] = (
    SheetColumn("name", "名称", True, "商品名称，必填，最多 128 个字", 24, limit=128),
    SheetColumn(
        "code",
        "代码",
        False,
        "企业的商品编码，表内不能重复。再次导入时按代码更新已有商品",
        16,
        limit=64,
        aliases=("编码", "商品代码", "商品编码"),
    ),
    SheetColumn("model", "型号", False, "例如 KFR-35GW", 16, limit=64),
    SheetColumn("spec", "规格", False, "例如：黑色 / L 码、1.5 匹", 18, limit=128),
    SheetColumn("category", "分类", False, "多级用 / 分隔，例如：家电/空调", 16, limit=128),
    SheetColumn(
        "image_url",
        "图片URL",
        False,
        "http 或 https 开头的图片链接",
        30,
        limit=1024,
        aliases=("图片 URL", "图片链接", "图片", "图片地址"),
    ),
    SheetColumn(
        "cost_price",
        "成本价",
        False,
        "只能填数字。只有有权限的员工能看到，不会告诉客户",
        12,
        money=True,
        aliases=("进价",),
    ),
    SheetColumn(
        "retail_price",
        "建议零售价",
        False,
        "只能填数字。AI 只会告诉客户这个价格；不填时 AI 答复价格以客服确认为准",
        12,
        money=True,
        aliases=("零售价", "售价", "价格"),
    ),
    SheetColumn("aliases", "别名", False, "客户常用的俗称、简称，多个用、分隔", 20),
    SheetColumn("remark", "备注", False, "内部备注，不给 AI，也不给客户", 24, limit=2000),
    SheetColumn("status", "状态", False, "上架或下架，不填为上架（下架的商品 AI 不推荐）", 10),
)
KEYS = tuple(c.key for c in COLUMNS)
EXAMPLE: dict[str, str] = {
    "name": "智能门锁 X1",
    "code": "LOCK-X1",
    "model": "X1",
    "spec": "黑色",
    "category": "智能家居/门锁",
    "image_url": "https://example.com/images/lock-x1.jpg",
    "cost_price": "800",
    "retail_price": "1299",
    "aliases": "指纹锁、电子锁",
    "remark": "示例行，导入前请删除",
    "status": "上架",
}
NOTES = (
    "第一行是表头，列的顺序可以调整；只有“名称”必填。",
    "再次上传时：有代码的按代码更新已有商品；没有代码的按“名称 + 型号 + 规格”匹配，"
    "匹配不到的新增。",
    "更新已有商品时，留空的列保持原来的值。",
    "价格列只能填数字（不带货币符号），最多两位小数。",
    "成本价和备注只有有权限的员工能看到，永远不会告诉客户或交给 AI。",
    "每次最多导入 5000 行；上传后先预览校验结果，确认后才写入商品库。",
)


def template() -> bytes:
    columns = [
        Column(
            title=f"{c.title}*" if c.required else c.title,
            width=c.width,
            prompt=(f"{c.title}（{'必填' if c.required else '选填'}）", c.hint),
            decimal_error="价格只能填不小于 0 的数字" if c.money else None,
        )
        for c in COLUMNS
    ]
    guide_rows: list[list[Cell]] = [
        [c.title, "必填" if c.required else "选填", c.hint] for c in COLUMNS
    ]
    guide_rows.append(["", "", ""])
    guide_rows += [["说明", "", note] for note in NOTES]
    guide = Sheet(
        "填写说明",
        [Column("列", 14), Column("是否必填", 10), Column("说明", 80)],
        rows=guide_rows,
        validate_rows=0,
    )
    return write_workbook([Sheet("商品", columns, rows=[[EXAMPLE[k] for k in KEYS]]), guide])


def _header_key(title: str) -> str | None:
    cleaned = re.sub(r"[\s*＊]|（必填）|\(必填\)|（选填）|\(选填\)", "", title).lower()
    for column in COLUMNS:
        names = (column.title, *column.aliases)
        if cleaned in {re.sub(r"\s", "", n).lower() for n in names}:
            return column.key
    return None


def parse_money(raw: str) -> Decimal:
    text = raw.strip().replace(",", "").replace("，", "")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("只能填数字") from exc
    if not value.is_finite():
        raise ValueError("只能填数字")
    if value < 0:
        raise ValueError("不能小于 0")
    value = value.quantize(CENT, rounding=ROUND_HALF_UP)
    if value > MAX_PRICE:
        raise ValueError("金额太大")
    return value


def parse_status(raw: str) -> str | None:
    text = raw.strip().lower()
    if text in ("", "上架", "on", "在售", "是"):
        return "on" if text else None
    if text in ("下架", "off", "停售", "否"):
        return "off"
    raise ValueError("只能填上架或下架")


@dataclass
class ParsedRow:
    row: int
    values: dict[str, str]
    problems: list[str] = field(default_factory=list)
    example: bool = False

    def cleaned(self) -> dict[str, Any]:
        """校验通过的行转成商品字段（空的选填列为 None，表示不修改）。"""
        v = self.values
        return {
            "name": v["name"],
            "code": v["code"] or None,
            "model": v["model"] or None,
            "spec": v["spec"] or None,
            "category": normalize_category(v["category"]) if v["category"] else None,
            "image_url": v["image_url"] or None,
            "cost_price": parse_money(v["cost_price"]) if v["cost_price"] else None,
            "retail_price": parse_money(v["retail_price"]) if v["retail_price"] else None,
            "aliases": split_aliases(v["aliases"]) if v["aliases"] else None,
            "remark": v["remark"] or None,
            "status": parse_status(v["status"]),
        }


def _check(row: ParsedRow) -> None:
    v = row.values
    if not v["name"]:
        row.problems.append("名称必填")
    for column in COLUMNS:
        value = v[column.key]
        if column.limit and len(value) > column.limit:
            row.problems.append(f"{column.title}最多 {column.limit} 个字")
        if column.money and value:
            try:
                parse_money(value)
            except ValueError as exc:
                row.problems.append(f"{column.title}{exc}")
    if v["image_url"] and not URL.match(v["image_url"]):
        row.problems.append("图片URL 要以 http:// 或 https:// 开头")
    if v["status"]:
        try:
            parse_status(v["status"])
        except ValueError as exc:
            row.problems.append(f"状态{exc}")


def read(filename: str, data: bytes) -> list[ParsedRow]:
    """解析上传的表格并逐行校验（不访问数据库）。表内重复的代码、重复的"名称 + 型号 + 规格"
    （没有代码时）标为有问题。"""
    if suffix(filename) not in (".xlsx", ".csv"):
        raise ParseError("商品表格支持 .xlsx 和 .csv（请使用下载的模板）")
    if len(data) > MAX_BYTES:
        raise ParseError("文件太大（最多 10 MB）")
    table = [[str(cell) for cell in row] for row in parse_sheet(filename, data)]
    table = [row for row in table if any(cell.strip() for cell in row)]
    if not table:
        raise ParseError("表格是空的")
    header = [_header_key(cell) for cell in table[0]]
    if "name" not in header:
        raise ParseError("第一行的表头里没有“名称”列（请使用下载的模板）")
    body = table[1:]
    if not body:
        raise ParseError("表格里没有商品")
    if len(body) > MAX_ROWS:
        raise ParseError(f"一次最多导入 {MAX_ROWS} 行，请分批上传")
    rows: list[ParsedRow] = []
    codes: dict[str, int] = {}
    identities: dict[tuple[str, str, str], int] = {}
    for offset, cells in enumerate(body, start=2):
        values = dict.fromkeys(KEYS, "")
        for index, key in enumerate(header):
            if key and index < len(cells):
                values[key] = cells[index].strip()
        row = ParsedRow(offset, values)
        if values == {**EXAMPLE}:
            row.example = True
            rows.append(row)
            continue
        _check(row)
        code = values["code"]
        if code:
            if code in codes:
                row.problems.append(f"代码与第 {codes[code]} 行重复")
            codes.setdefault(code, offset)
        elif values["name"]:
            identity = (values["name"].lower(), values["model"].lower(), values["spec"].lower())
            if identity in identities:
                row.problems.append(f"与第 {identities[identity]} 行重复（名称、型号、规格相同）")
            identities.setdefault(identity, offset)
        rows.append(row)
    return rows


def result_workbook(rows: list[dict[str, Any]]) -> bytes:
    """导入结果：每一行的处理结果（新增、更新、跳过及原因）。"""
    outcome = {"create": "新增", "update": "更新", "skip": "跳过"}
    return write_workbook(
        [
            Sheet(
                "导入结果",
                [
                    Column("行号", 8),
                    Column("名称", 24),
                    Column("代码", 16),
                    Column("结果", 10),
                    Column("说明", 60),
                ],
                rows=[
                    [
                        r["row"],
                        r["values"].get("name", ""),
                        r["values"].get("code", ""),
                        outcome.get(r.get("result") or r.get("action") or "", ""),
                        "；".join(r.get("problems") or []),
                    ]
                    for r in rows
                ],
                validate_rows=0,
            )
        ]
    )
