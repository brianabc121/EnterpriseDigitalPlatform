"""商品库的 Excel 模板与逐行校验（设计文档 §25.2、§25.12、§25.13）。

模板第一行是表头，第二、三行是示例（一个成品、一个材料）；每一列选中时显示填写说明，价格、库存列
只能填不小于 0 的数字（成品的库存只能是整数，材料最多三位小数），类别、状态、现货从下拉列表里选；
另有"填写说明"工作表。上传的表格按表头名称识别列（顺序不限，"名称*""名称（必填）"都可以），示例行
原样保留时自动跳过。只更新库存时可以只有"代码"和"库存"（或"数量"）两列：按代码更新已有商品。
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
MAX_STOCK = 100_000_000
CENT = Decimal("0.01")
MILLI = Decimal("0.001")
URL = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)
KINDS = {"成品": "goods", "商品": "goods", "产品": "goods", "材料": "material", "原料": "material"}
KIND_NAMES = {"goods": "成品", "material": "材料"}


@dataclass(frozen=True)
class SheetColumn:
    key: str
    title: str
    required: bool
    hint: str
    width: int = 14
    money: bool = False
    # 数量（库存、预警值）：不小于 0，成品只能是整数，材料最多三位小数。
    quantity: bool = False
    limit: int = 0
    aliases: tuple[str, ...] = ()
    choices: tuple[str, ...] = ()


COLUMNS: tuple[SheetColumn, ...] = (
    SheetColumn("name", "名称", True, "商品或材料的名称，必填，最多 128 个字", 24, limit=128),
    SheetColumn(
        "code",
        "代码",
        False,
        "企业的商品编码，表内不能重复。再次导入时按代码更新已有商品",
        16,
        limit=64,
        aliases=("编码", "商品代码", "商品编码", "材料代码", "材料编码"),
    ),
    SheetColumn(
        "kind",
        "类别",
        False,
        "成品（可以销售）或材料（只用于生产领料，不给 AI、不能下单）；留空按上传时的选择。"
        "已有商品的类别不能修改",
        8,
        aliases=("商品类别", "物料类别", "类型"),
        choices=("成品", "材料"),
    ),
    SheetColumn("model", "型号", False, "例如 KFR-35GW", 16, limit=64),
    SheetColumn("spec", "规格", False, "例如：黑色 / L 码、1.5 匹", 18, limit=128),
    SheetColumn(
        "unit",
        "单位",
        False,
        "例如 件、套、米、公斤；库存和领料都按这个单位",
        8,
        limit=16,
        aliases=("计量单位",),
    ),
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
    SheetColumn(
        "status",
        "状态",
        False,
        "上架或下架，不填为上架（下架的商品 AI 不推荐）",
        10,
        choices=("上架", "下架"),
    ),
    SheetColumn(
        "ready_made",
        "现货",
        False,
        "是：直接从成品库存发货，不需要加工；否或不填：下单后加工。材料不填",
        8,
        aliases=("是否现货",),
        choices=("是", "否"),
    ),
    SheetColumn(
        "stock",
        "库存",
        False,
        "只能填数字（成品填整数，材料最多三位小数）。上传时选择“盘点”（这个数就是现有库存）"
        "或“入库”（加到现有库存上）；留空表示不修改库存",
        10,
        quantity=True,
        aliases=("数量", "库存数量", "现有库存", "入库数量"),
    ),
    SheetColumn(
        "stock_alert",
        "库存预警",
        False,
        "只能填数字。可用库存不高于这个数时，列表里标为库存不足",
        10,
        quantity=True,
        aliases=("预警库存", "安全库存", "库存预警值"),
    ),
)
KEYS = tuple(c.key for c in COLUMNS)
EXAMPLES: tuple[dict[str, str], ...] = (
    {
        "name": "智能门锁 X1",
        "code": "LOCK-X1",
        "kind": "成品",
        "model": "X1",
        "spec": "黑色",
        "unit": "把",
        "category": "智能家居/门锁",
        "image_url": "https://example.com/images/lock-x1.jpg",
        "cost_price": "800",
        "retail_price": "1299",
        "aliases": "指纹锁、电子锁",
        "remark": "示例行，导入前请删除",
        "status": "上架",
        "ready_made": "是",
        "stock": "100",
        "stock_alert": "10",
    },
    {
        "name": "铝合金型材",
        "code": "AL-6063",
        "kind": "材料",
        "model": "6063",
        "spec": "银白",
        "unit": "米",
        "category": "原材料/型材",
        "image_url": "",
        "cost_price": "35",
        "retail_price": "",
        "aliases": "",
        "remark": "示例行，导入前请删除",
        "status": "上架",
        "ready_made": "",
        "stock": "120.5",
        "stock_alert": "20",
    },
)
EXAMPLE = EXAMPLES[0]
NOTES = (
    "第一行是表头，列的顺序可以调整；只有“名称”必填。",
    "类别：成品可以销售；材料只用于生产（开领料单），不给 AI，也不能下单。已有商品的类别不能修改。",
    "再次上传时：有代码的按代码更新已有商品；没有代码的按“名称 + 型号 + 规格”匹配，"
    "匹配不到的新增。",
    "更新已有商品时，留空的列保持原来的值。",
    "价格列只能填数字（不带货币符号），最多两位小数。",
    "成本价和备注只有有权限的员工能看到，永远不会告诉客户或交给 AI。",
    "现货：直接从成品库存发货，不需要加工；其他成品下单后由工人领料加工、入库再发货。",
    "库存：上传时选择“盘点”（表格里的数就是现有库存）或“入库”（加到现有库存上），需要有调整"
    "库存的权限；成品填整数，材料最多三位小数；留空的不修改库存。材料总是管理库存，没有填的从 0 "
    "开始。",
    "只更新库存时，表格可以只有“代码”和“库存”（或“数量”）两列，按代码更新已有商品。",
    "每次最多导入 5000 行；上传后先预览校验结果，确认后才写入商品库。",
)


def _column(c: SheetColumn) -> Column:
    error = None
    if c.money:
        error = "价格只能填不小于 0 的数字"
    elif c.quantity:
        error = f"{c.title}只能填不小于 0 的数字（成品填整数）"
    return Column(
        title=f"{c.title}*" if c.required else c.title,
        width=c.width,
        prompt=(f"{c.title}（{'必填' if c.required else '选填'}）", c.hint),
        decimal_error=error,
        choices=c.choices,
    )


def template() -> bytes:
    columns = [_column(c) for c in COLUMNS]
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
    rows: list[list[Cell]] = [[example[k] for k in KEYS] for example in EXAMPLES]
    return write_workbook([Sheet("商品", columns, rows=rows), guide])


def export_workbook(products: list[Any], *, cost: bool) -> bytes:
    """导出商品库（与模板的列相同，改完可以直接再导入）。没有查看成本价的权限时不含成本价列
    （再导入时成本价保持原值）。"""
    columns = [c for c in COLUMNS if cost or c.key != "cost_price"]

    def value(product: Any, key: str) -> Cell:
        if key in ("cost_price", "retail_price"):
            price = getattr(product, key)
            return f"{price:.2f}" if price is not None else ""
        if key == "aliases":
            return "、".join(product.aliases or [])
        if key == "status":
            return "上架" if product.status == "on" else "下架"
        if key == "kind":
            return KIND_NAMES.get(product.kind, "成品")
        if key == "ready_made":
            return "是" if product.ready_made else ("" if product.kind == "material" else "否")
        if key in ("stock", "stock_alert"):
            return quantity_text(getattr(product, key))
        raw = getattr(product, key)
        return "" if raw is None else str(raw)

    return write_workbook(
        [
            Sheet(
                "商品",
                [_column(c) for c in columns],
                rows=[[value(p, c.key) for c in columns] for p in products],
                validate_rows=max(len(products) + 100, 1000),
            )
        ]
    )


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


def parse_stock(raw: str) -> Decimal:
    """库存、预警值：不小于 0 的数字，最多三位小数（成品只能是整数，另外检查）。"""
    text = raw.strip().replace(",", "").replace("，", "")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("只能填数字") from exc
    if not value.is_finite():
        raise ValueError("只能填数字")
    if value < 0:
        raise ValueError("不能小于 0")
    if value > MAX_STOCK:
        raise ValueError("数量太大")
    if value != value.quantize(MILLI):
        raise ValueError("最多三位小数")
    return value.quantize(MILLI)


def quantity_text(value: Decimal | None) -> str:
    """表格里的数量：去掉多余的 0（2.500 → 2.5，10.000 → 10）。"""
    if value is None:
        return ""
    text = f"{value.normalize():f}"
    return "0" if text in ("-0", "0") else text


def parse_kind(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    if text in KINDS:
        return KINDS[text]
    if text.lower() in ("goods", "material"):
        return text.lower()
    raise ValueError("只能填成品或材料")


def parse_yes(raw: str) -> bool | None:
    text = raw.strip().lower()
    if not text:
        return None
    if text in ("是", "y", "yes", "true", "1", "现货", "有"):
        return True
    if text in ("否", "n", "no", "false", "0", "不是", "无"):
        return False
    raise ValueError("只能填是或否")


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
        """校验通过的行转成商品字段（空的列为 None，表示不修改）。"""
        v = self.values
        return {
            "name": v["name"] or None,
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
            "kind": parse_kind(v["kind"]),
            "unit": v["unit"] or None,
            "ready_made": parse_yes(v["ready_made"]),
            "stock": parse_stock(v["stock"]) if v["stock"] else None,
            "stock_alert": parse_stock(v["stock_alert"]) if v["stock_alert"] else None,
        }


def _check(row: ParsedRow) -> None:
    v = row.values
    # 只更新库存等字段时可以只填代码（按代码更新已有商品；代码不存在时在预览里提示）。
    if not v["name"] and not v["code"]:
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
        if column.quantity and value:
            try:
                parse_stock(value)
            except ValueError as exc:
                row.problems.append(f"{column.title}{exc}")
    if v["image_url"] and not URL.match(v["image_url"]):
        row.problems.append("图片URL 要以 http:// 或 https:// 开头")
    if v["status"]:
        try:
            parse_status(v["status"])
        except ValueError as exc:
            row.problems.append(f"状态{exc}")
    for key, title, parse in (("kind", "类别", parse_kind), ("ready_made", "现货", parse_yes)):
        if v[key]:
            try:
                parse(v[key])
            except ValueError as exc:
                row.problems.append(f"{title}{exc}")


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
    if "name" not in header and not ("code" in header and "stock" in header):
        raise ParseError(
            "第一行的表头里没有“名称”列（请使用下载的模板；只更新库存时可以只有“代码”和“库存”两列）"
        )
    present = {key for key in header if key}
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
        # 原样保留的示例行（旧版模板没有的列不参与比较）。
        if "name" in present and any(
            all(values[key] == example[key] for key in present) for example in EXAMPLES
        ):
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


def _stored(value: Any) -> str:
    """导入记录里保存的数量（字符串）显示时去掉多余的 0。"""
    if value is None:
        return "—"
    return quantity_text(Decimal(str(value)))


def stock_note(row: dict[str, Any]) -> str:
    """导入的一行对库存的修改，如"库存 10 → 15"（原来不管理库存的写作"—"）。"""
    if row.get("stock_after") is None:
        return ""
    return f"库存 {_stored(row.get('stock_before'))} → {_stored(row['stock_after'])}"


def result_workbook(rows: list[dict[str, Any]]) -> bytes:
    """导入结果：每一行的处理结果（新增、更新、跳过及原因，以及库存的变化）。"""
    outcome = {"create": "新增", "update": "更新", "skip": "跳过"}

    def note(row: dict[str, Any]) -> str:
        parts = list(row.get("problems") or [])
        if (row.get("result") or row.get("action")) in ("create", "update") and stock_note(row):
            parts.append(stock_note(row))
        return "；".join(parts)

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
                        note(r),
                    ]
                    for r in rows
                ],
                validate_rows=0,
            )
        ]
    )
