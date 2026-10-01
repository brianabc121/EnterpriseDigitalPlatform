"""开单时的商品联想（设计文档 §25.16）：检索键（统一写法、拼音全拼和首字母）、打分排序（完全一致
> 开头一致 > 包含 > 拼音 > 相近，几个词组合、整段代码）、只是有点像的不列、常用的和有库存的靠前、
自己最近用过的、权限；修改和导入商品时更新检索键；商品库和仓库列表的搜索也认拼音首字母和不同写法。"""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.products import suggest
from app.modules.products.models import Product, ProductKind
from tests.desk import Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_orders import call, customer, new_order
from tests.test_warehouse import adjust, goods, material, sheet, worker

PRODUCTS = "/api/v1/products"
WAREHOUSE = "/api/v1/warehouse"


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


def product(
    name: str, code: str, *, model: str = "", spec: str = "", category: str = "", aliases: Any = ()
) -> Product:
    return Product(
        name=name,
        code=code,
        model=model,
        spec=spec,
        category=category,
        aliases=list(aliases),
        kind=ProductKind.GOODS,
    )


CATALOG = [
    product("铝合金窗", "WIN-01", spec="1.2m×1.5m", category="门窗"),
    product("铝合金窗", "WIN-03", spec="1.5m×1.8m", category="门窗"),
    product("防盗门", "DOOR-01", category="门窗"),
    product("纱窗", "NET-01", category="门窗", aliases=["窗纱"]),
    product("吸顶灯", "LAMP-01", model="LED-36", spec="36W", category="灯具"),
    product("铝合金型材", "AL-6063", category="型材"),
    product("密封条", "SEAL-1", category="辅料"),
    product("不锈钢螺丝", "SCR-4", model="M4", category="辅料", aliases=["螺钉"]),
]


def ranked(query: str) -> list[tuple[str, str, str | None]]:
    """按分数排好的（代码、匹配方式、字段），去掉低于下限的。"""
    scores = suggest.score_all(query, CATALOG)
    floor = suggest.keep([s.score for s in scores])
    found = sorted(
        ((s, p) for s, p in zip(scores, CATALOG, strict=True) if s.score >= floor),
        key=lambda sp: -sp[0].score,
    )
    return [(p.code or "", s.match, s.field) for s, p in found]


def test_normalized_keys_and_pinyin() -> None:
    assert suggest.normalize("ＷＩＮ－01") == "win 01"
    assert suggest.normalize("1.2m×1.5m") == "1.2m 1.5m"
    assert suggest.normalize("1.2*1.5") == suggest.normalize("1.2 x 1.5") == "1.2 1.5"
    assert suggest.compact("WIN-01") == "win01"
    assert suggest.tokens("1.5 铝合金窗，1.5") == ["1.5", "铝合金窗"]
    assert suggest.pinyin("铝合金窗") == ("lvhejinchuang", "lhjc")
    assert suggest.pinyin("智能门锁 X1") == ("zhinengmensuox1", "znmsx1")

    key = suggest.build_search_key("铝合金窗", ["断桥窗"], "WIN-01", "", "1.2m×1.5m", "门窗/铝窗")
    for part in ("铝合金窗", "win 01", "win01", "1.2m 1.5m", "1.2m1.5m", "门窗 铝窗", "dqc"):
        assert part in key, part
    # 全拼里的 ü 写作 v，也可以输入 u。
    for part in ("lhjc", "lvhejinchuang", "luhejinchuang"):
        assert part in key.split(" "), part


def test_scoring_and_ranking() -> None:
    # 拼音首字母、全拼（ü 输入 u 也可以）。
    assert ranked("lhjc")[:2] == [("WIN-01", "pinyin", "pinyin"), ("WIN-03", "pinyin", "pinyin")]
    assert {c for c, *_ in ranked("lu")} == {"WIN-01", "WIN-03", "AL-6063", "SCR-4"}
    assert ranked("mft") == [("SEAL-1", "pinyin", "pinyin")]
    # 代码：不分大小写，忽略分隔符；整段一致的排在最前面，相邻的代码列为相近，其他"-01"的不列。
    assert (
        ranked("win0")
        == ranked("WIN-0")
        == [
            ("WIN-01", "prefix", "code"),
            ("WIN-03", "prefix", "code"),
        ]
    )
    assert (
        ranked("WIN-01")
        == ranked("win01")
        == [
            ("WIN-01", "exact", "code"),
            ("WIN-03", "similar", "code"),
        ]
    )
    assert ranked("XYZ-01") == []
    assert ranked("m4") == [("SCR-4", "exact", "model")]
    assert ranked("LED 36")[0] == ("LAMP-01", "exact", "model")
    # 规格的不同写法；规格加名称。
    assert ranked("1.2*1.5")[0] == ("WIN-01", "prefix", "spec")
    assert ranked("1.2*1.5")[1] == ("WIN-03", "similar", "spec")
    assert ranked("36w 灯") == [("LAMP-01", "exact", "spec")]
    # 名称：开头一致、包含，俗称、分类。
    assert [c for c, *_ in ranked("铝")] == ["WIN-01", "WIN-03", "AL-6063"]
    assert ranked("窗")[0] == ("NET-01", "prefix", "alias")
    assert ranked("螺钉") == [("SCR-4", "exact", "alias")]
    assert {(c, m, f) for c, m, f in ranked("门窗")} == {
        (c, "exact", "category") for c in ("WIN-01", "WIN-03", "DOOR-01", "NET-01")
    }
    # 少字、错字：相近。
    assert ranked("铝窗")[0] == ("WIN-01", "similar", "name")
    assert [c for c, m, _ in ranked("铝合金床") if m == "similar"][:3] == [
        "WIN-01",
        "WIN-03",
        "AL-6063",
    ]
    assert ranked("不锈钢 螺母") == [("SCR-4", "similar", "name")]
    # 客户的说法：商品库里没有的词（颜色、数量）不计入，但不算完全找到。
    assert ranked("铝合金窗 1.2*1.5 白色 2樘")[0] == ("WIN-01", "similar", "name")
    # 单个字母：代码的开头和拼音首字母。
    assert ranked("l")[0] == ("LAMP-01", "prefix", "code")
    assert ("WIN-01", "pinyin", "pinyin") in ranked("l")
    assert ranked("dmc") == []


async def test_suggest_products_while_ordering(desk: Desk) -> None:
    window = await goods(desk, "WIN-01", "铝合金窗", spec="1.2m×1.5m", category="门窗")
    wide = await goods(desk, "WIN-03", "铝合金窗", spec="1.5m×1.8m", category="门窗")
    door = await goods(desk, "DOOR-01", "防盗门", category="门窗")
    await goods(desk, "NET-01", "纱窗", aliases=["窗纱"], category="门窗")
    await material(desk, "AL-6063", "铝合金型材")
    off = await desk.client.post(
        PRODUCTS,
        headers=desk.admin,
        json={"code": "WIN-09", "name": "铝合金窗", "status": "off"},
    )
    assert off.status_code == 201, off.text

    async def suggest_(q: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        found: dict[str, Any] = await call(
            desk, headers or desk.admin, "GET", f"{PRODUCTS}/suggest?q={q}&limit=5"
        )
        return found

    def codes(found: dict[str, Any]) -> list[tuple[str, str, str | None]]:
        return [(i["product"]["code"], i["match"], i["field"]) for i in found["items"]]

    # 只找上架的成品（材料、下架的不列）；返回完整的商品信息（价格、库存）。
    found = await suggest_("lhjc")
    assert (found["recent"], codes(found)) == (
        False,
        [("WIN-01", "pinyin", "pinyin"), ("WIN-03", "pinyin", "pinyin")],
    )
    assert found["items"][0]["product"]["retail_price"] == "100.00"
    assert found["items"][0]["score"] == pytest.approx(0.855)
    assert codes(await suggest_("win0")) == [
        ("WIN-01", "prefix", "code"),
        ("WIN-03", "prefix", "code"),
    ]
    exact = await suggest_("WIN-01")
    assert codes(exact) == [("WIN-01", "exact", "code"), ("WIN-03", "similar", "code")]
    assert exact["items"][0]["score"] == 1.05
    assert codes(await suggest_("1.2*1.5"))[0] == ("WIN-01", "prefix", "spec")
    assert codes(await suggest_("铝窗"))[0][1:] == ("similar", "name")
    # 完全一致的在前，有一半的字相同的列为相近。
    assert codes(await suggest_("窗纱")) == [
        ("NET-01", "exact", "alias"),
        ("WIN-01", "similar", "name"),
        ("WIN-03", "similar", "name"),
    ]
    assert codes(await suggest_("XYZ-01")) == []

    # 没有输入：自己最近下单用过的，最近用的在前；别人下的单不算。
    assert await suggest_("") == {"items": [], "recent": True}
    customer_id = await customer(desk)
    await new_order(desk, desk.admin, customer_id, [{"product_id": wide["id"], "quantity": 1}])
    await new_order(desk, desk.admin, customer_id, [{"product_id": door["id"], "quantity": 2}])
    recent = await suggest_("")
    assert (recent["recent"], codes(recent)) == (
        True,
        [("DOOR-01", "recent", None), ("WIN-03", "recent", None)],
    )
    alice = await desk.agent("alice", online=False)
    assert await suggest_("", alice.headers) == {"items": [], "recent": True}
    # 分数一样时常用的在前（WIN-03 下过单）。
    assert codes(await suggest_("铝合金窗", alice.headers))[:2] == [
        ("WIN-03", "exact", "name"),
        ("WIN-01", "exact", "name"),
    ]

    # 修改商品后检索键随之更新。
    renamed = await desk.client.put(
        f"{PRODUCTS}/{window['id']}",
        headers=desk.admin,
        json={"code": "WIN-01", "name": "断桥铝合金窗", "spec": "1.2m×1.5m", "retail_price": "100"},
    )
    assert renamed.status_code == 200, renamed.text
    assert codes(await suggest_("dqlhjc")) == [("WIN-01", "pinyin", "pinyin")]

    # 商品库的搜索也认拼音首字母、不同写法的代码和规格、几个词组合。
    async def listed(q: str) -> list[str]:
        page = await call(desk, desk.admin, "GET", f"{PRODUCTS}?q={q}")
        return sorted(p["code"] for p in page["items"])

    assert await listed("lhjc") == ["WIN-01", "WIN-03", "WIN-09"]
    assert await listed("win01") == ["WIN-01"]
    assert await listed("1.2*1.5") == ["WIN-01"]
    assert await listed("门窗 fdm") == ["DOOR-01"]

    # 权限：工人看不到商品库（最早的工人兼任仓管，看第二个工人）。
    await worker(desk, "wang")
    zhao = await worker(desk, "zhao")
    denied = await desk.client.get(f"{PRODUCTS}/suggest?q=lhjc", headers=zhao.headers)
    assert denied.status_code == 403


async def test_suggest_items_while_entering_documents(desk: Desk) -> None:
    await material(desk, "AL-6063", "铝合金型材")
    seal = await material(desk, "SEAL-1", "密封条")
    screw = await desk.client.post(
        PRODUCTS,
        headers=desk.admin,
        json={"code": "SCR-4", "name": "不锈钢螺丝", "model": "M4", "kind": "material",
              "aliases": ["螺钉"], "unit": "个"},
    )  # fmt: skip
    assert screw.status_code == 201, screw.text
    await goods(desk, "LAMP-01", "吸顶灯", ready_made=True)
    await adjust(desk, seal["id"], "set", 10)

    async def suggest_(
        q: str, kind: str = "material", headers: dict[str, str] | None = None
    ) -> Any:
        return await call(
            desk, headers or desk.admin, "GET", f"{WAREHOUSE}/suggest?kind={kind}&q={q}"
        )

    def codes(found: dict[str, Any]) -> list[tuple[str, str, str | None]]:
        return [(i["item"]["code"], i["match"], i["field"]) for i in found["items"]]

    found = await suggest_("mft")
    assert codes(found) == [("SEAL-1", "pinyin", "pinyin")]
    item = found["items"][0]["item"]
    assert (item["stock"], item["stock_available"], item["unit"]) == (10, 10, "米")
    assert "retail_price" not in item
    assert codes(await suggest_("螺钉")) == [("SCR-4", "exact", "alias")]
    assert codes(await suggest_("M4")) == [("SCR-4", "exact", "model")]
    assert codes(await suggest_("6063")) == [("AL-6063", "prefix", "code")]
    assert codes(await suggest_("xdd", "goods")) == [("LAMP-01", "pinyin", "pinyin")]
    assert codes(await suggest_("xdd")) == []
    # 分数一样时有库存的在前。
    await material(desk, "SEAL-2", "密封条")
    assert codes(await suggest_("密封条")) == [
        ("SEAL-1", "exact", "name"),
        ("SEAL-2", "exact", "name"),
    ]

    # 没有输入：自己最近开单用过的。
    assert await suggest_("") == {"items": [], "recent": True}
    await call(
        desk, desk.admin, "POST", f"{WAREHOUSE}/documents", 201,
        kind="requisition", lines=[{"product_id": seal["id"], "quantity": 2}],
    )  # fmt: skip
    assert codes(await suggest_("")) == [("SEAL-1", "recent", None)]
    wang = await worker(desk, "wang")
    assert await suggest_("", headers=wang.headers) == {"items": [], "recent": True}
    assert codes(await suggest_("lhjxc", headers=wang.headers)) == [("AL-6063", "pinyin", "pinyin")]

    # 仓库列表的搜索同样认拼音首字母。
    page = await call(desk, desk.admin, "GET", f"{WAREHOUSE}/items?kind=material&q=mft")
    assert sorted(i["code"] for i in page["items"]) == ["SEAL-1", "SEAL-2"]

    # 导入的商品也有检索键。
    content = sheet(["名称*", "代码", "类别", "单位"], [["钢化玻璃", "GL-5", "材料", "平方米"]])
    preview = await call(
        desk, desk.admin, "POST", f"{PRODUCTS}/imports", 201,
        filename="材料.xlsx", content_base64=content, stock_mode="set", default_kind="material",
    )  # fmt: skip
    await call(desk, desk.admin, "POST", f"{PRODUCTS}/imports/{preview['id']}/confirm")
    assert codes(await suggest_("ghbl")) == [("GL-5", "pinyin", "pinyin")]

    # 权限：坐席不能开仓库的单。
    alice = await desk.agent("alice", online=False)
    denied = await desk.client.get(f"{WAREHOUSE}/suggest?q=mft", headers=alice.headers)
    assert denied.status_code == 403
