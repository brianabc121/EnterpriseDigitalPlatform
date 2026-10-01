"""开单时的商品联想（设计文档 §25.16）：检索键、粗筛条件和打分。

- 检索键（products.search_key）：名称、俗称、代码、型号、规格、分类统一写法后的内容（另存一份
  去掉分隔符的写法），加上名称和俗称的全拼和拼音首字母，用空格隔开。新建、修改、导入、同步商品时
  和检索词项一起更新。
- 联想：先用检索键在数据库里粗筛出候选（`prefilter`），再逐个打分（`score_all`）：输入的每个词在
  各字段里找最好的匹配（完全一致 > 开头一致 > 包含 > 拼音 > 相近），乘以字段的权重，几个词取平均；
  整段输入和代码或型号完全一致的排在最前面。分数接近时，常用的、有库存的靠前（由调用方加上）。
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from pypinyin import lazy_pinyin
from sqlalchemy import ColumnElement, and_, case, literal, or_

from app.modules.products.models import Product

SuggestField = Literal["code", "model", "name", "alias", "spec", "category", "pinyin"]
SuggestMatch = Literal["exact", "prefix", "contains", "pinyin", "similar"]

# 字段的权重：代码、名称最高，俗称、型号、规格、拼音其次，分类最低。
WEIGHT: dict[SuggestField, float] = {
    "code": 1.0,
    "name": 1.0,
    "alias": 0.95,
    "model": 0.95,
    "spec": 0.9,
    "pinyin": 0.9,
    "category": 0.8,
}
EXACT, PREFIX, CONTAINS = 1.0, 0.9, 0.8
# 低于这个分数的不推荐，也不推荐低于最高分 RELATIVE 倍的；低于 STRONG 的标为"相近"。
MIN_SCORE = 0.35
RELATIVE = 0.5
STRONG = 0.6
# 粗筛：最多看几个词、每个词最多几个字，候选最多几个。
MAX_TOKENS = 8
MAX_CHARS = 8
CANDIDATES = 300

_CJK = "㐀-䶿一-鿿豈-﫿"
_TOKEN = re.compile(rf"[{_CJK}]+|[a-z0-9]+(?:\.[0-9]+)*[a-z0-9]*")
_CJK_ONLY = re.compile(rf"^[{_CJK}]+$")
_RUNS = re.compile(rf"[{_CJK}]+|[^{_CJK}]+")
# 数字之间的乘号："1.2×1.5""1.2*1.5""1.2x1.5"都按两个数字处理。
_TIMES = re.compile(r"(?<=[0-9])\s*[x×*]\s*(?=[0-9])")
_NOT_WORD = re.compile(rf"[^a-z0-9.{_CJK}]+")


def normalize(text: str) -> str:
    """统一写法：全角转半角、小写，数字之间的乘号和各种分隔符换成空格。"""
    lowered = unicodedata.normalize("NFKC", text).lower()
    return " ".join(_NOT_WORD.sub(" ", _TIMES.sub(" ", lowered)).split())


def compact(text: str) -> str:
    """去掉空格和分隔符的写法（"WIN-01"→ win01），比较代码、型号时用。"""
    return normalize(text).replace(" ", "")


def tokens(text: str) -> list[str]:
    """输入拆成词：连续的汉字是一个词，字母和数字是一个词（"1.5""36w"）；去重，保持先后。"""
    found = _TOKEN.findall(normalize(text))
    return list(dict.fromkeys(t.strip(".") for t in found if t.strip(".")))[:MAX_TOKENS]


@lru_cache(maxsize=50_000)
def pinyin(text: str) -> tuple[str, str]:
    """全拼和拼音首字母，字母和数字原样保留（"智能门锁 X1"→ zhinengmensuox1、znmsx1）。"""
    full: list[str] = []
    initials: list[str] = []
    for part in _RUNS.findall(normalize(text)):
        if _is_cjk(part):
            words = [w for w in lazy_pinyin(part) if w]
            full.extend(words)
            initials.extend(w[0] for w in words)
        else:
            plain = part.replace(" ", "")
            full.append(plain)
            initials.append(plain)
    return "".join(full), "".join(initials)


def _pinyin_keys(text: str) -> list[str]:
    full, initials = pinyin(text)
    # ü 写作 v（lv 铝），也可以输入 u。
    return [k for k in dict.fromkeys([full, full.replace("v", "u"), initials]) if k]


def build_search_key(
    name: str,
    aliases: Sequence[str],
    code: str | None,
    model: str,
    spec: str,
    category: str,
) -> str:
    """检索键：数据库里粗筛候选时用（迁移给已有商品补上时也用这个函数）。"""
    texts = [name, *aliases, code or "", model, spec, category.replace("/", " ")]
    parts: list[str] = []
    for text in texts:
        if text:
            parts.extend([normalize(text), compact(text)])
    for text in [name, *aliases]:
        if _has_cjk(text):
            parts.extend(_pinyin_keys(text))
    return " ".join(dict.fromkeys(p for p in parts if p))[:4000]


def search_key(product: Product) -> str:
    return build_search_key(
        product.name,
        product.aliases or [],
        product.code,
        product.model or "",
        product.spec or "",
        product.category or "",
    )


def _has_cjk(text: str) -> bool:
    return any("㐀" <= ch <= "鿿" or "豈" <= ch <= "﫿" for ch in text)


def _is_cjk(token: str) -> bool:
    return bool(_CJK_ONLY.match(token))


def prefilter(query: str) -> tuple[ColumnElement[bool], ColumnElement[int]] | None:
    """数据库里的粗筛条件和粗排序：每个词出现在检索键里（汉字词另外按单字，字母数字词另外按开头三个字符，
    容许少字和错字）。没有可用的词时返回 None。"""
    words = tokens(query)
    if not words:
        return None
    conditions: list[ColumnElement[bool]] = []
    ranks: list[ColumnElement[int]] = []

    def add(piece: str, rank: int) -> None:
        condition = Product.search_key.contains(piece, autoescape=True)
        conditions.append(condition)
        ranks.append(case((condition, rank), else_=0))

    for word in words:
        add(word, 3)
        if _is_cjk(word) and len(word) > 1:
            for ch in list(dict.fromkeys(word))[:MAX_CHARS]:
                add(ch, 1)
        elif not _is_cjk(word) and len(word) >= 4:
            add(word[:3], 1)
    rank: ColumnElement[int] = literal(0)
    for part in ranks:
        rank = rank + part
    return or_(*conditions), rank


def keyword_condition(query: str) -> ColumnElement[bool] | None:
    """列表的搜索（商品库、仓库、批量选择）：每个词都出现在检索键里（认拼音首字母和不同写法）。"""
    words = tokens(query)
    if not words:
        return None
    return and_(*(Product.search_key.contains(w, autoescape=True) for w in words))


@dataclass(frozen=True)
class Fields:
    """打分用的各字段（统一写法后的）。"""

    code: str
    model: str
    name: str
    aliases: tuple[str, ...]
    spec: str
    category: str
    pinyins: tuple[tuple[str, str], ...] = field(default=())

    @classmethod
    def of(cls, product: Product) -> "Fields":
        names = [product.name, *(product.aliases or [])]
        return cls(
            code=normalize(product.code or ""),
            model=normalize(product.model or ""),
            name=normalize(product.name),
            aliases=tuple(normalize(a) for a in product.aliases or [] if a),
            spec=normalize(product.spec or ""),
            category=normalize((product.category or "").replace("/", " ")),
            pinyins=tuple(pinyin(n) for n in names if n and _has_cjk(n)),
        )


@dataclass(frozen=True)
class Scored:
    score: float
    field: SuggestField | None
    match: SuggestMatch


NOTHING = Scored(0.0, None, "similar")


def _plain(token: str, value: str) -> tuple[float, SuggestMatch] | None:
    if not value:
        return None
    squeezed = value.replace(" ", "")
    if token in (value, squeezed):
        return EXACT, "exact"
    if value.startswith(token) or squeezed.startswith(token):
        return PREFIX, "prefix"
    if any(word.startswith(token) for word in value.split()):
        return PREFIX - 0.05, "prefix"
    if token in value or token in squeezed:
        return CONTAINS, "contains"
    return None


def _subsequence(token: str, value: str) -> bool:
    """token 的字按顺序都出现在 value 里（"铝窗"之于"铝合金窗"）。"""
    rest = iter(value)
    return all(ch in rest for ch in token)


def _near_cjk(token: str, value: str) -> float | None:
    """汉字的相近程度：按顺序都出现（少字）或者大部分字相同（错字、多字）。"""
    if len(token) < 2 or not value:
        return None
    if _subsequence(token, value):
        return 0.7
    chars = set(token)
    ratio = len(chars & set(value)) / len(chars)
    return 0.35 + 0.35 * ratio if ratio >= 0.5 else None


def _distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _near_latin(token: str, value: str) -> float | None:
    """字母和数字的相近程度：错一个字符（较长时错两个），和整段或同样长的开头比。"""
    if len(token) < 3 or not value:
        return None
    squeezed = value.replace(" ", "")
    allowed = 1 if len(token) < 6 else 2
    candidates = [squeezed, squeezed[: len(token)], *value.split()]
    if any(_distance(token, c) <= allowed for c in candidates if c):
        return 0.6
    return None


def _pinyin_match(token: str, pinyins: tuple[tuple[str, str], ...]) -> float | None:
    if _is_cjk(token):
        return None
    best: float | None = None
    for full, initials in pinyins:
        variants = (full, full.replace("v", "u"))
        if token == initials:
            score = 0.95
        elif len(token) >= 2 and initials.startswith(token):
            score = 0.85
        elif len(token) >= 2 and any(v.startswith(token) for v in variants):
            score = 0.8
        elif len(token) >= 3 and (token in initials or any(token in v for v in variants)):
            score = 0.6
        elif len(token) == 1 and initials.startswith(token):
            score = 0.5
        else:
            continue
        best = max(best or 0.0, score)
    return best


def _token(token: str, f: Fields) -> Scored:
    """一个词在各字段里最好的匹配。"""
    best = NOTHING

    def offer(score: float, where: SuggestField, how: SuggestMatch) -> None:
        nonlocal best
        weighted = score * WEIGHT[where]
        if weighted > best.score:
            best = Scored(weighted, where, how)

    plain: list[tuple[SuggestField, str]] = [
        ("code", f.code),
        ("model", f.model),
        ("name", f.name),
        *(("alias", a) for a in f.aliases),
        ("spec", f.spec),
        ("category", f.category),
    ]
    # 单个字母或数字太常见：只认代码、型号的开头和拼音首字母。
    single = len(token) == 1 and not _is_cjk(token)
    for where, value in plain:
        found = _plain(token, value)
        if found is None:
            continue
        score, how = found
        if single and not (where in ("code", "model") and how in ("exact", "prefix")):
            continue
        offer(score, where, how)
    pinyin_score = _pinyin_match(token, f.pinyins)
    if pinyin_score is not None:
        offer(pinyin_score, "pinyin", "pinyin")
    if best.score < STRONG:
        for where, value in plain:
            near = _near_cjk(token, value) if _is_cjk(token) else _near_latin(token, value)
            if near is not None:
                offer(near, where, "similar")
    return best


def _weak(token: str) -> bool:
    """单个字母或数字、一两位的数字（"01""4"）：只靠它们对上的不算找到。"""
    return not _is_cjk(token) and (len(token) == 1 or (len(token) <= 2 and token.isdigit()))


def _whole(whole: str, f: Fields) -> Scored | None:
    """整段输入去掉空格和分隔符后和代码、型号比较（"WIN-01""win 01"都是 win01）。"""
    found: list[Scored] = []
    pairs: tuple[tuple[SuggestField, str], ...] = (("code", f.code), ("model", f.model))
    for where, value in pairs:
        squeezed = value.replace(" ", "")
        if not whole or not squeezed:
            continue
        if whole == squeezed:
            return Scored(1.05, where, "exact")
        if _has_cjk(whole):
            continue
        if len(whole) >= 2 and squeezed.startswith(whole):
            found.append(Scored(PREFIX * WEIGHT[where], where, "prefix"))
        elif len(whole) >= 4:
            allowed = 1 if len(whole) < 8 else 2
            if min(_distance(whole, squeezed), _distance(whole, squeezed[: len(whole)])) <= allowed:
                found.append(Scored(0.6 * WEIGHT[where], where, "similar"))
    return max(found, key=lambda s: s.score, default=None)


def score_all(query: str, products: Sequence[Product]) -> list[Scored]:
    """几个商品和输入的匹配程度（0 到约 1.05）：每个词在各字段里最好的匹配取平均。

    - 哪个商品都对不上的词（客户说法里的颜色、数量、单位等）不计入；
    - 只靠一两位数字、单个字母对上的（"WIN-01"里的"01"之于 DOOR-01）不算找到，除非只输入了这些；
    - 整段输入和代码、型号完全一致的排在最前面，整段输入是代码、型号的开头或只差一个字符的也算。
    """
    words = tokens(query)
    fields = [Fields.of(p) for p in products]
    rows = [[_token(w, f) for w in words] for f in fields]
    useful = [i for i in range(len(words)) if any(row[i].score > 0 for row in rows)]
    weak = set() if all(_weak(w) for w in words) else {i for i in useful if _weak(words[i])}
    whole, name = compact(query), normalize(query)
    results: list[Scored] = []
    for row, f in zip(rows, fields, strict=True):
        by_code = _whole(whole, f)
        if by_code is not None and by_code.match == "exact":
            results.append(by_code)
            continue
        if name and name == f.name:
            results.append(Scored(1.0, "name", "exact"))
            continue
        counted = [row[i] for i in useful]
        strong_words = [row[i] for i in useful if i not in weak and row[i].score > 0]
        total = sum(s.score for s in counted) / len(counted) if strong_words else 0.0
        lead = max(strong_words or counted, key=lambda s: s.score, default=NOTHING)
        # 输入的每个词都找到了、没有只是相近的，才标出按什么找到的；否则是"相近"。
        complete = len(useful) == len(words) and all(
            s.score > 0 and s.match != "similar" for s in counted
        )
        result = Scored(round(total, 4), lead.field, lead.match if complete else "similar")
        if by_code is not None and by_code.score > result.score:
            result = by_code
        results.append(result)
    return results


def keep(scores: Sequence[float]) -> float:
    """列出来的最低分数：不低于 MIN_SCORE，也不低于最高分的一半（有完全一致的代码时，只是有点像的
    就不列了）。"""
    return max(MIN_SCORE, max(scores, default=0.0) * RELATIVE) - 1e-9
