"""表单知识的写法：叫法的统一写法，知识写成一句话（设计文档 §25.18）。"""

from decimal import Decimal

from app.modules.formkb.models import FormKbEntry, Kind
from app.modules.products import stock, suggest
from app.modules.products.models import Product

# 叫法：不到 2 个字符、纯数字、超过 32 个字符的输入不学。
MIN_CHARS = 2
MAX_CHARS = 32
LABEL_CHARS = 64


def alias_key(text: str | None) -> str | None:
    """叫法的统一写法（§25.16：全角半角、大小写、分隔符、数字之间的乘号），去掉空格；不学的为空。"""
    if not text:
        return None
    key = suggest.compact(text)
    if len(key) < MIN_CHARS or len(key) > MAX_CHARS or key.replace(".", "").isdigit():
        return None
    return key


def label_of(text: str) -> str:
    return " ".join(text.split())[:LABEL_CHARS]


def product_label(product: Product) -> str:
    return " ".join(part for part in (product.name, product.spec) if part)


def quantity(value: Decimal, unit: str) -> str:
    return f"{stock.fmt(value)} {unit}".strip()


def sentence(
    entry: FormKbEntry,
    product: Product,
    related: Product | None,
    *,
    recipe: Decimal | None = None,
    counts: bool = True,
) -> str:
    """一条知识写成一句话（counts：搭配带上一起开的次数）。"""
    if entry.kind == Kind.ALIAS:
        return f"输入「{entry.label or entry.text}」→ {product_label(product)}"
    assert related is not None
    if entry.kind == Kind.USAGE:
        text = f"{product_label(product)} 每{product.unit or '件'}用 {related.name}"
        if entry.value is not None:
            text += f" {quantity(entry.value, related.unit)}"
        if recipe is not None and recipe != entry.value:
            text += f"（配方 {quantity(recipe, related.unit)}）"
        return text
    text = f"开 {product_label(product)} 时常一起开 {product_label(related)}"
    forms, together = entry.stats.get("forms"), entry.stats.get("together")
    if counts and isinstance(forms, int) and isinstance(together, int) and forms:
        text += f"（{forms} 张里 {together} 张）"
    return text
