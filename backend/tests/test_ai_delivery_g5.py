"""AI 回答的投递与反馈：正在输入、分段发送、按渠道的 AI 参数、访客评价 AI 回答。"""

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.ai.segments import split_reply
from tests.desk import Desk, Visitor
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_ai_reception import ANSWER, bot_texts, decisions, enable_ai

LONG = (
    "发货说明：第一步，下单后仓库会在二十四小时内打包，打包完成后短信通知您快递单号。"
    "第二步，您可以在订单详情页查看物流轨迹，一般两到三天送达，偏远地区五到七天。"
    "第三步，签收时请当面检查外包装，如有破损可以拒收并联系我们重新发货。\n"
    "温馨提示：节假日期间快递量较大，送达时间可能延后一到两天；需要指定送达时间的，"
    "请在下单备注里写明，我们会尽量安排。大件商品由物流公司送货上门，需要提前电话预约。"
)


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    desk = await Desk(app, client, fake_im, settings, database_urls).open()
    await enable_ai(desk)
    return desk


def test_split_reply() -> None:
    assert split_reply("好的，一般 2 到 3 天送达。") == ["好的，一般 2 到 3 天送达。"]
    parts = split_reply(LONG)
    assert 2 <= len(parts) <= 4
    assert all(len(p) <= 150 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == LONG.replace("\n", "")


async def _faq(desk: Desk, title: str, content: str) -> str:
    response = await desk.client.post(
        "/api/v1/kb/items",
        headers=desk.admin,
        json={"title": title, "content": content, "publish": True},
    )
    assert response.status_code == 201, response.text
    item_id: str = response.json()["id"]
    return item_id


async def test_long_answers_are_segmented_after_typing(desk: Desk) -> None:
    await _faq(desk, "发货和送达有什么说明？", LONG)
    visitor = await desk.visitor()
    await desk.say(visitor, "发货和送达有什么说明")
    texts = bot_texts(desk, visitor)
    assert texts == split_reply(LONG) and len(texts) >= 2
    assert {"type": "typing", "sender": "bot", "name": ""} in desk.im.group_signals(
        visitor.group_id
    )
    # 关闭分段后整段发送，也不再提示正在输入。
    await desk.client.put(
        "/api/v1/ai/settings", headers=desk.admin, json={"segment_replies": False}
    )
    other = await desk.visitor()
    await desk.say(other, "发货和送达有什么说明")
    assert bot_texts(desk, other) == [LONG]
    assert desk.im.group_signals(other.group_id) == []


async def _channel(desk: Desk) -> str:
    channels = (await desk.client.get("/api/v1/channels", headers=desk.admin)).json()["items"]
    channel_id: str = channels[0]["id"]
    return channel_id


async def test_channel_overrides_ai_thresholds(desk: Desk) -> None:
    await desk.agent("alice")
    channel_id = await _channel(desk)
    visitor = await desk.visitor()
    await desk.say(visitor, "快递大概几天能送到")
    assert bot_texts(desk, visitor) == [ANSWER]

    patched = await desk.client.patch(
        f"/api/v1/channels/{channel_id}",
        headers=desk.admin,
        json={"ai": {"relevance_threshold": 0.99, "handoff_threshold": 0.3}},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["ai"] == {
        "handoff_threshold": 0.3,
        "relevance_threshold": 0.99,
        "max_turns": None,
    }
    other = await desk.visitor()
    await desk.say(other, "快递大概几天能送到")
    chat = await desk.session_of(other)
    assert (chat["status"], chat["handoff_reason"]) == ("human_serving", "score")
    [decision] = await decisions(desk, other)
    assert decision["signals"]["low_relevance"] is True

    unknown = await desk.client.patch(
        f"/api/v1/channels/{channel_id}",
        headers=desk.admin,
        json={"kb_space_ids": ["00000000-0000-0000-0000-000000000001"]},
    )
    assert unknown.status_code == 422
    cleared = await desk.client.patch(
        f"/api/v1/channels/{channel_id}", headers=desk.admin, json={"ai": {}}
    )
    assert cleared.json()["ai"]["handoff_threshold"] is None


async def _bot_message_id(desk: Desk, visitor: Visitor) -> str:
    [row] = await desk.sql(
        "SELECT channel_msg_id FROM messages WHERE room_id = $1 AND sender_type = 'bot'",
        visitor.room_id,
    )
    message_id: str = row["channel_msg_id"]
    return message_id


async def test_visitors_rate_ai_answers(desk: Desk) -> None:
    visitor = await desk.visitor()
    await desk.say(visitor, "快递几天能到")
    server_msg_id = await _bot_message_id(desk, visitor)
    headers = {"X-Visitor-Token": visitor.visitor_token}

    disliked = await desk.client.post(
        "/api/v1/visitor/ai-feedback",
        headers=headers,
        json={"server_msg_id": server_msg_id, "value": -1},
    )
    assert disliked.status_code == 204, disliked.text
    [item] = await desk.sql("SELECT visitor_likes, visitor_dislikes FROM kb_items")
    assert (item["visitor_likes"], item["visitor_dislikes"]) == (0, 1)
    # 改为有用：计数随之调整；重复提交不重复计数。
    for _ in range(2):
        liked = await desk.client.post(
            "/api/v1/visitor/ai-feedback",
            headers=headers,
            json={"server_msg_id": server_msg_id, "value": 1},
        )
        assert liked.status_code == 204
    [item] = await desk.sql("SELECT visitor_likes, visitor_dislikes FROM kb_items")
    assert (item["visitor_likes"], item["visitor_dislikes"]) == (1, 0)

    # 只能评价自己会话里智能客服的回答。
    other = await desk.visitor()
    stranger = await desk.client.post(
        "/api/v1/visitor/ai-feedback",
        headers={"X-Visitor-Token": other.visitor_token},
        json={"server_msg_id": server_msg_id, "value": 1},
    )
    assert stranger.status_code == 404
