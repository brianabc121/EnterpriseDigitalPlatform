"""AI 公司助理（设计文档 §27.3、§27.4）：接入机器人（Telegram、飞书、钉钉、WhatsApp、企业微信）、
回调验签、绑定码对应员工、通过助理发送平台提醒、控制台对话与按权限的查询工具、群聊记录与知识提炼。"""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.integrations.imbots import dingtalk, whatsapp
from app.integrations.wecom.crypto import CallbackCrypto
from app.modules.assistant.extraction import run_group_extraction
from tests.desk import Agent, Desk
from tests.fake_bots import TELEGRAM_USERNAME, FakeBots
from tests.fake_openim import FakeOpenIM
from tests.fake_wecom import AES_KEY
from tests.support import DatabaseUrls
from tests.test_ai_gateway_g5 import _tools_provider
from tests.test_tasks import call, create

TG_TOKEN = "123456:telegram-token"


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


@pytest.fixture
async def tools_desk(desk: Desk, app: FastAPI) -> Desk:
    """默认供应商支持工具调用（运营后台登记的供应商）。"""
    await _tools_provider(desk, app)
    return desk


# ---- 辅助 ----


async def add_bot(desk: Desk, **payload: Any) -> dict[str, Any]:
    bot: dict[str, Any] = await call(
        desk, desk.admin, "POST", "/api/v1/assistant/bots", 201, **payload
    )
    return bot


def hook_path(bot: dict[str, Any]) -> str:
    return urlsplit(bot["webhook_url"]).path


def hook_token(bot: dict[str, Any]) -> str:
    return hook_path(bot).rsplit("/", 1)[1]


_message_ids = iter(range(1000, 100000))


def tg_update(
    user_id: int,
    text: str,
    *,
    chat_type: str = "private",
    chat_id: int | None = None,
    title: str = "",
    name: str = "Mei",
) -> dict[str, Any]:
    return {
        "update_id": next(_message_ids),
        "message": {
            "message_id": next(_message_ids),
            "from": {"id": user_id, "first_name": name, "is_bot": False},
            "chat": {"id": chat_id or user_id, "type": chat_type, "title": title},
            "date": int(time.time()),
            "text": text,
        },
    }


async def telegram_says(
    desk: Desk,
    bot: dict[str, Any],
    user_id: int,
    text: str,
    *,
    token: str | None = None,
    **extra: Any,
) -> httpx.Response:
    response = await desk.client.post(
        hook_path(bot),
        headers={"X-Telegram-Bot-Api-Secret-Token": token or hook_token(bot)},
        json=tg_update(user_id, text, **extra),
    )
    if response.status_code == 200:
        await desk.flush()
    return response


async def binding_code(desk: Desk, staff: Agent) -> str:
    issued = await call(desk, staff.headers, "POST", "/api/v1/assistant/binding-code")
    code: str = issued["code"]
    assert len(code) == 6 and issued["hint"].endswith(code)
    return code


async def add_telegram(desk: Desk) -> dict[str, Any]:
    return await add_bot(
        desk, provider="telegram", name="小助", config={}, secrets={"bot_token": TG_TOKEN}
    )


async def bind_telegram(desk: Desk, bot: dict[str, Any], staff: Agent, user_id: int) -> None:
    code = await binding_code(desk, staff)
    await telegram_says(desk, bot, user_id, f"绑定 {code}")


async def tool_log(desk: Desk) -> list[list[str]]:
    rows = await desk.sql(
        "SELECT tools FROM assistant_messages WHERE role = 'assistant' ORDER BY created_at, id"
    )
    return [
        json.loads(r["tools"]) if isinstance(r["tools"], str) else list(r["tools"]) for r in rows
    ]


# ---- Telegram：接入、绑定、提问 ----


async def test_telegram_bot_binds_staff_and_answers_with_their_permissions(
    tools_desk: Desk, fake_bots: FakeBots
) -> None:
    desk = tools_desk
    providers = await call(desk, desk.admin, "GET", "/api/v1/assistant/providers")
    assert {p["provider"] for p in providers["items"]} == {
        "wecom", "dingtalk", "feishu", "telegram", "whatsapp"
    }  # fmt: skip
    assert providers["webhook_base"] == "http://localhost:8000/hooks/assistant/"

    # 添加：校验 Bot Token、登记回调地址（带随机令牌），密钥不回显。
    bot = await add_telegram(desk)
    assert bot["config"] == {"bot_username": TELEGRAM_USERNAME}
    assert bot["secret_keys"] == ["bot_token"]
    assert bot["webhook_url"].startswith(f"http://localhost:8000/hooks/assistant/{bot['id']}/")
    assert fake_bots.webhooks == [
        {"url": bot["webhook_url"], "secret_token": hook_token(bot), "allowed_updates": ["message"]}
    ]
    rejected = await desk.client.post(
        "/api/v1/assistant/bots",
        headers=desk.admin,
        json={"provider": "telegram", "name": "坏的", "secrets": {"bot_token": "bad:token"}},
    )
    assert rejected.status_code == 422, rejected.text

    # 未绑定的人只收到绑定说明；令牌不对的回调被拒绝。
    mei = await desk.agent("mei", roles=["agent"], online=False)
    assert (await telegram_says(desk, bot, 1001, "你好")).status_code == 200
    assert fake_bots.texts("telegram", "1001") == [
        "你好，我是小助。请先绑定员工账号：在控制台「AI 助理 → 我的绑定」获取 6 位绑定码，"
        "然后在这里回复「绑定 123456」。"
    ]
    forbidden = await telegram_says(desk, bot, 1001, "你好", token="wrong")
    assert forbidden.status_code == 403
    assert len(fake_bots.texts("telegram")) == 1

    # 绑定码：错的被拒绝，对的绑定成功（一次性）。
    code = await binding_code(desk, mei)
    await telegram_says(desk, bot, 1001, "绑定 000000")
    assert fake_bots.texts("telegram", "1001")[-1].startswith("绑定码不对")
    await telegram_says(desk, bot, 1001, f"绑定 {code}")
    assert fake_bots.texts("telegram", "1001")[-1].startswith("绑定成功，Mei 你好")
    identities = await call(desk, desk.admin, "GET", "/api/v1/assistant/identities")
    [identity] = identities["items"]
    assert (identity["external_user_id"], identity["staff_name"], identity["display_name"]) == (
        "1001",
        "Mei",
        "Mei",
    )
    await telegram_says(desk, bot, 1001, f"绑定 {code}")
    assert fake_bots.texts("telegram", "1001")[-1].startswith("绑定码不对")

    # 提问：以本人的权限查工具（个人待办）。
    await create(desk, mei.headers, "回访张总")
    await telegram_says(desk, bot, 1001, "我的待办有什么")
    answer = fake_bots.texts("telegram", "1001")[-1]
    assert "回访张总" in answer and "未完成 1 条" in answer
    assert await tool_log(desk) == [["list_my_tasks"]]
    mine = await call(desk, mei.headers, "GET", "/api/v1/assistant/bindings")
    assert [b["bot_name"] for b in mine["items"]] == ["小助"]
    assert [b["name"] for b in mine["bots"]] == ["小助"]
    listed = await call(desk, desk.admin, "GET", "/api/v1/assistant/bots")
    assert listed["items"][0]["identities"] == 1

    # 限流：每人每分钟的次数用完后不再调用模型。
    await call(
        desk, desk.admin, "PUT", "/api/v1/assistant/settings",
        enabled=True, name="小助", persona="", group_reply_mode="silent", group_extraction=True,
        notify_enabled=True, per_minute=1,
    )  # fmt: skip
    await telegram_says(desk, bot, 1001, "我的待办")
    assert fake_bots.texts("telegram", "1001")[-1] == "提问太频繁了，请稍后再试。"

    # 员工自己解除绑定后又只能收到绑定说明。
    await call(
        desk, mei.headers, "DELETE", f"/api/v1/assistant/bindings/{mine['items'][0]['id']}", 204
    )
    await telegram_says(desk, bot, 1001, "我的待办")
    assert fake_bots.texts("telegram", "1001")[-1].startswith("你好，我是小助。请先绑定")


async def test_platform_reminders_reach_bound_staff_through_the_assistant(
    desk: Desk, fake_bots: FakeBots
) -> None:
    bot = await add_telegram(desk)
    mei = await desk.agent("mei", roles=["agent"], online=False)
    await bind_telegram(desk, bot, mei, 1001)
    before = len(fake_bots.texts("telegram", "1001"))

    # 交办个人待办：站内信之外，助理也发一条（附控制台链接）。
    await create(desk, desk.admin, "整理本周报价单", owner_id=str(mei.staff_id))
    await desk.flush()
    sent = fake_bots.texts("telegram", "1001")[before:]
    assert len(sent) == 1
    assert sent[0].startswith("管理员 交办：整理本周报价单\n")
    assert "http://localhost:5173/tasks?id=" in sent[0]

    # 客户待办分派也经同一个出口。
    types = await call(desk, desk.admin, "GET", "/api/v1/todo-types")
    other = next(t for t in types["items"] if t["code"] == "other")
    await call(
        desk, desk.admin, "POST", "/api/v1/todos", 201,
        type_id=other["id"], title="寄资料", assignee_id=str(mei.staff_id),
    )  # fmt: skip
    await desk.flush()
    assert any(
        "新待办" in t and "寄资料" in t for t in fake_bots.texts("telegram", "1001")[before:]
    )

    # 关闭"通过助理发送通知"后不再发；没有绑定的人（管理员）从来收不到。
    await call(
        desk, desk.admin, "PUT", "/api/v1/assistant/settings",
        enabled=True, name="小助", persona="", group_reply_mode="silent", group_extraction=True,
        notify_enabled=False, per_minute=20,
    )  # fmt: skip
    count = len(fake_bots.texts("telegram"))
    await create(desk, desk.admin, "再交办一件", owner_id=str(mei.staff_id))
    await desk.flush()
    assert len(fake_bots.texts("telegram")) == count
    assert fake_bots.texts("telegram", target="admin") == []

    # 发送失败不影响业务，记在机器人上。
    fake_bots.failing.add("telegram")
    await call(
        desk, desk.admin, "PUT", "/api/v1/assistant/settings",
        enabled=True, name="小助", persona="", group_reply_mode="silent", group_extraction=True,
        notify_enabled=True, per_minute=20,
    )  # fmt: skip
    await create(desk, desk.admin, "发不出去的", owner_id=str(mei.staff_id))
    await desk.flush()
    detail = await call(desk, desk.admin, "GET", f"/api/v1/assistant/bots/{bot['id']}")
    assert detail["failures"] == 1 and "Forbidden" in detail["last_error"]


# ---- 控制台对话与权限 ----


async def test_console_chat_uses_only_the_tools_the_staff_may_use(tools_desk: Desk) -> None:
    desk = tools_desk
    mei = await desk.agent("mei", roles=["agent"], online=False)
    wang = await desk.agent("wang", roles=["worker"], online=False)
    await create(desk, mei.headers, "回访张总")
    await create(desk, wang.headers, "打磨把手")

    reply = await call(desk, mei.headers, "POST", "/api/v1/assistant/chat", text="我的待办")
    assert "回访张总" in reply["reply"] and reply["tools"] == ["list_my_tasks"]
    history = await call(desk, mei.headers, "GET", "/api/v1/assistant/chat/history")
    assert [(m["role"], m["tools"]) for m in history["items"]] == [
        ("user", []),
        ("assistant", ["list_my_tasks"]),
    ]

    # 工人没有订单权限：模型拿不到订单工具，只能查自己的待办。
    denied = await call(
        desk, wang.headers, "POST", "/api/v1/assistant/chat", text="订单 TD20261001-0001 怎么样了"
    )
    assert denied["tools"] == [] and denied["reply"].startswith("我可以帮你")
    own = await call(desk, wang.headers, "POST", "/api/v1/assistant/chat", text="我的待办")
    assert "打磨把手" in own["reply"] and "回访张总" not in own["reply"]

    # 记一件事、完成一件事。
    noted = await call(
        desk, mei.headers, "POST", "/api/v1/assistant/chat", text="记一下：明天给供应商打电话"
    )
    assert noted["tools"] == ["create_task"] and "已记下" in noted["reply"]
    tasks = await call(desk, mei.headers, "GET", "/api/v1/tasks?view=mine&status=open")
    created = next(t for t in tasks["items"] if t["title"] == "明天给供应商打电话")
    assert created["source"] == "assistant"
    finished = await call(
        desk, mei.headers, "POST", "/api/v1/assistant/chat", text="完成了 回访张总"
    )
    assert finished["tools"] == ["complete_task"] and "已完成" in finished["reply"]
    assert (await call(desk, mei.headers, "GET", f"/api/v1/tasks/{tasks['items'][-1]['id']}"))[
        "status"
    ] in (
        "done",
        "open",
    )
    done_rows = await desk.sql("SELECT title FROM staff_tasks WHERE status = 'done'")
    assert [r["title"] for r in done_rows] == ["回访张总"]

    # 管理员问团队概览。
    team = await call(desk, desk.admin, "POST", "/api/v1/assistant/chat", text="大家手上都有什么事")
    assert team["tools"] == ["team_overview"] and "Wang：未完成 1 条" in team["reply"]

    # 没有 assistant:use 的角色不能对话；关闭助理后谁都不能。
    await call(
        desk,
        desk.admin,
        "POST",
        "/api/v1/roles",
        201,
        code="guest",
        name="访客",
        permissions=["dashboard:view"],
    )
    guest = await desk.agent("guest", roles=["guest"], online=False)
    await call(desk, guest.headers, "POST", "/api/v1/assistant/chat", 403, text="你好")
    await call(desk, wang.headers, "GET", "/api/v1/assistant/settings", 403)
    await call(
        desk, desk.admin, "PUT", "/api/v1/assistant/settings",
        enabled=False, name="小助", persona="", group_reply_mode="silent", group_extraction=True,
        notify_enabled=True, per_minute=20,
    )  # fmt: skip
    off = await call(desk, mei.headers, "POST", "/api/v1/assistant/chat", text="我的待办")
    assert off["reply"].startswith("AI 助理还没有启用")


async def test_without_tool_calling_the_assistant_only_searches_knowledge(desk: Desk) -> None:
    await call(
        desk, desk.admin, "POST", "/api/v1/kb/items", 201,
        title="周末发货吗", content="周末正常发货，周日下午 4 点前的订单当天发出。", publish=True,
    )  # fmt: skip
    mei = await desk.agent("mei", roles=["agent"], online=False)
    reply = await call(desk, mei.headers, "POST", "/api/v1/assistant/chat", text="周末发货吗")
    assert reply["tools"] == ["search_knowledge"] and "周日下午 4 点前" in reply["reply"]
    nothing = await call(desk, mei.headers, "POST", "/api/v1/assistant/chat", text="今天天气")
    assert nothing["reply"].startswith("知识库里没有找到相关资料")
    wang = await desk.agent("wang", roles=["worker"], online=False)
    worker = await call(desk, wang.headers, "POST", "/api/v1/assistant/chat", text="周末发货吗")
    assert worker["reply"].startswith("当前模型不支持查询")


# ---- 飞书：群聊记录与知识提炼 ----


def feishu_event(
    open_id: str,
    text: str,
    *,
    chat_id: str,
    chat_type: str = "group",
    mentions: list[dict[str, Any]] | None = None,
    ago: timedelta = timedelta(minutes=20),
) -> dict[str, Any]:
    created = int((datetime.now(UTC) - ago).timestamp() * 1000)
    return {
        "schema": "2.0",
        "header": {
            "event_id": uuid.uuid4().hex,
            "event_type": "im.message.receive_v1",
            "token": "vt",
            "create_time": str(created),
        },
        "event": {
            "sender": {"sender_id": {"open_id": open_id}, "sender_type": "user"},
            "message": {
                "message_id": f"om_{next(_message_ids)}",
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "create_time": str(created),
                "mentions": mentions or [],
            },
        },
    }


async def feishu_says(desk: Desk, bot: dict[str, Any], event: dict[str, Any]) -> httpx.Response:
    response = await desk.client.post(hook_path(bot), json=event)
    if response.status_code == 200:
        await desk.flush()
    return response


async def test_feishu_groups_are_recorded_silently_and_mined_for_knowledge(
    tools_desk: Desk, fake_bots: FakeBots
) -> None:
    desk = tools_desk
    bot = await add_bot(
        desk,
        provider="feishu",
        name="飞书助理",
        config={"app_id": "cli_demo"},
        secrets={"app_secret": "s", "verification_token": "vt"},
    )
    # 配置回调地址时的验证；Verification Token 不对的被拒绝。
    challenge = await desk.client.post(
        hook_path(bot), json={"type": "url_verification", "token": "vt", "challenge": "abc"}
    )
    assert (challenge.status_code, challenge.json()) == (200, {"challenge": "abc"})
    bad = await desk.client.post(
        hook_path(bot), json={"type": "url_verification", "token": "no", "challenge": "abc"}
    )
    assert bad.status_code == 403

    # 群里的消息只记录，不说话。
    for open_id, text in (
        ("ou_a", "周末可以发货吗？"),
        ("ou_b", "可以，周日下午4点前的订单当天发出。"),
        ("ou_a", "收到"),
    ):
        assert (
            await feishu_says(desk, bot, feishu_event(open_id, text, chat_id="oc_1"))
        ).status_code == 200
    assert fake_bots.sent == []
    groups = await call(desk, desk.admin, "GET", "/api/v1/assistant/groups")
    [group] = groups["items"]
    assert (group["message_count"], group["unextracted"], group["recording"], group["extract"]) == (
        3,
        3,
        True,
        True,
    )
    assert group["reply_mode"] is None and group["bot_name"] == "飞书助理"
    messages = await call(
        desk, desk.admin, "GET", f"/api/v1/assistant/groups/{group['id']}/messages"
    )
    assert messages["total"] == 3 and messages["items"][0]["text"] == "收到"
    # 同一条消息重复推送不重复记录。
    duplicate = feishu_event("ou_a", "再来一次", chat_id="oc_1")
    await feishu_says(desk, bot, duplicate)
    await feishu_says(desk, bot, duplicate)
    assert (await call(desk, desk.admin, "GET", "/api/v1/assistant/groups"))["items"][0][
        "message_count"
    ] == 4

    # 调度任务提炼：脱敏、匿名后进入审核台，来源"群聊"；提炼过的不再提炼。
    report = await run_group_extraction(desk.ctx)
    assert (report.tenants, report.groups, report.messages, report.candidates, report.failed) == (
        1,
        1,
        4,
        1,
        0,
    )
    candidates = await call(desk, desk.admin, "GET", "/api/v1/kb/candidates")
    [candidate] = candidates["items"]
    assert (candidate["question"], candidate["source"]) == ("周末可以发货吗？", "group")
    detail = await call(desk, desk.admin, "GET", f"/api/v1/kb/candidates/{candidate['id']}")
    assert [line["role"] for line in detail["evidence"][0]["lines"]] == ["同事1", "同事2"]
    again = await run_group_extraction(desk.ctx)
    assert again.messages == 0
    mined = (await call(desk, desk.admin, "GET", "/api/v1/assistant/groups"))["items"][0]
    assert (mined["unextracted"], mined["extracted_candidates"]) == (0, 1)
    assert mined["last_extracted_at"] is not None

    # 刚发的消息还没沉淀，调度不提炼；"立即提炼"不等。
    await feishu_says(
        desk, bot, feishu_event("ou_b", "发票几天能开好？", chat_id="oc_1", ago=timedelta(0))
    )
    assert (await run_group_extraction(desk.ctx)).messages == 0
    forced = await call(desk, desk.admin, "POST", f"/api/v1/assistant/groups/{group['id']}/extract")
    assert (forced["messages"], forced["candidates"], forced["error"]) == (1, 1, None)
    gaps = [
        c
        for c in (await call(desk, desk.admin, "GET", "/api/v1/kb/candidates"))["items"]
        if c["kind"] == "gap"
    ]
    assert [g["question"] for g in gaps] == ["发票几天能开好？"]

    # 关闭提炼或暂停记录后不再处理。
    paused = await call(
        desk,
        desk.admin,
        "PATCH",
        f"/api/v1/assistant/groups/{group['id']}",
        recording=False,
        extract=False,
    )
    assert (paused["recording"], paused["extract"]) == (False, False)
    await feishu_says(desk, bot, feishu_event("ou_a", "暂停期间的消息？", chat_id="oc_1"))
    assert (await call(desk, desk.admin, "GET", "/api/v1/assistant/groups"))["items"][0][
        "message_count"
    ] == 5

    # 被 @ 时回答：提问人要先绑定（私聊里用绑定码）。
    mei = await desk.agent("mei", roles=["agent"], online=False)
    await create(desk, mei.headers, "回访张总")
    code = await binding_code(desk, mei)
    await feishu_says(
        desk, bot, feishu_event("ou_a", f"绑定 {code}", chat_id="oc_p2p_a", chat_type="p2p")
    )
    assert fake_bots.texts("feishu", "chat_id:oc_p2p_a")[-1].startswith("绑定成功")
    await call(
        desk,
        desk.admin,
        "PATCH",
        f"/api/v1/assistant/groups/{group['id']}",
        recording=True,
        reply_mode="mentioned",
    )
    mention = [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "飞书助理"}]
    await feishu_says(
        desk, bot, feishu_event("ou_a", "@_user_1 我的待办", chat_id="oc_1", mentions=mention)
    )
    assert "回访张总" in fake_bots.texts("feishu", "chat_id:oc_1")[-1]
    # 没有绑定的人 @ 不回答；没有 @ 的也不回答。
    count = len(fake_bots.texts("feishu", "chat_id:oc_1"))
    await feishu_says(
        desk, bot, feishu_event("ou_b", "@_user_1 我的待办", chat_id="oc_1", mentions=mention)
    )
    await feishu_says(desk, bot, feishu_event("ou_a", "我的待办", chat_id="oc_1"))
    assert len(fake_bots.texts("feishu", "chat_id:oc_1")) == count
    # 记录的消息里对应到了员工。
    rows = await desk.sql(
        "SELECT count(*) AS n FROM assistant_group_messages WHERE staff_id = $1", mei.staff_id
    )
    assert rows[0]["n"] >= 1

    # 清空记录。
    cleared = await call(desk, desk.admin, "POST", f"/api/v1/assistant/groups/{group['id']}/clear")
    assert (cleared["message_count"], cleared["unextracted"]) == (0, 0)
    # 改回按租户设置。
    reset = await call(
        desk, desk.admin, "PATCH", f"/api/v1/assistant/groups/{group['id']}", clear_reply_mode=True
    )
    assert reset["reply_mode"] is None


# ---- 钉钉、WhatsApp、企业微信 ----


async def test_dingtalk_whatsapp_and_wecom_callbacks_are_verified(
    tools_desk: Desk, fake_bots: FakeBots
) -> None:
    desk = tools_desk
    mei = await desk.agent("mei", roles=["agent"], online=False)
    await create(desk, mei.headers, "回访张总")

    # 钉钉：HMAC 验签；回复发到回调里的 sessionWebhook；主动通知走单聊接口。
    ding = await add_bot(
        desk,
        provider="dingtalk",
        name="钉钉助理",
        config={"app_key": "ding123"},
        secrets={"app_secret": "dsecret"},
    )

    async def ding_says(text: str, *, sign: str | None = None) -> httpx.Response:
        timestamp = str(int(time.time() * 1000))
        response = await desk.client.post(
            hook_path(ding),
            headers={"timestamp": timestamp, "sign": sign or dingtalk.sign("dsecret", timestamp)},
            json={
                "msgtype": "text",
                "text": {"content": text},
                "senderStaffId": "u1",
                "senderNick": "小李",
                "conversationType": "1",
                "conversationId": "cid1",
                "msgId": f"m{next(_message_ids)}",
                "createAt": int(time.time() * 1000),
                "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=abc",
            },
        )
        if response.status_code == 200:
            await desk.flush()
        return response

    assert (await ding_says("你好", sign="bad")).status_code == 403
    assert (await ding_says("你好")).status_code == 200
    assert fake_bots.texts("dingtalk", "session")[-1].startswith("你好，我是小助")
    code = await binding_code(desk, mei)
    await ding_says(f"绑定 {code}")
    await ding_says("我的待办")
    assert "回访张总" in fake_bots.texts("dingtalk", "session")[-1]
    tested = await call(
        desk, desk.admin, "POST", f"/api/v1/assistant/bots/{ding['id']}/test", text="测试"
    )
    assert (tested["ok"], tested["sent"]) == (True, 1)
    assert fake_bots.texts("dingtalk", "cid1") == ["测试"]

    # WhatsApp：GET 验证 Verify Token，POST 验签 X-Hub-Signature-256。
    wa = await add_bot(
        desk,
        provider="whatsapp",
        name="WhatsApp",
        config={"phone_number_id": "111"},
        secrets={"access_token": "at", "app_secret": "as", "verify_token": "vt"},
    )
    verified = await desk.client.get(
        hook_path(wa),
        params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "123"},
    )
    assert (verified.status_code, verified.text) == (200, "123")
    assert (
        await desk.client.get(
            hook_path(wa),
            params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"},
        )
    ).status_code == 403
    payload = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "contacts": [
                                    {"wa_id": "8613800001111", "profile": {"name": "Mei"}}
                                ],
                                "messages": [
                                    {
                                        "id": "wamid.in1",
                                        "from": "8613800001111",
                                        "timestamp": str(int(time.time())),
                                        "type": "text",
                                        "text": {"body": "你好"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()
    unsigned = await desk.client.post(
        hook_path(wa), content=payload, headers={"Content-Type": "application/json"}
    )
    assert unsigned.status_code == 403
    signed = await desk.client.post(
        hook_path(wa),
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": whatsapp.signature("as", payload),
        },
    )
    assert signed.status_code == 200
    await desk.flush()
    assert fake_bots.texts("whatsapp", "8613800001111")[-1].startswith("你好，我是小助")

    # 企业微信智能机器人：AES 验签与解密；成员按 wecom_userid 自动对应；回复发到 response_url。
    wecom_bot = await add_bot(
        desk,
        provider="wecom",
        name="企微助理",
        config={},
        secrets={"token": "tok", "encoding_aes_key": AES_KEY},
    )
    crypto = CallbackCrypto("tok", AES_KEY)
    echostr = crypto.encrypt("hello", "corp")
    query = {"timestamp": "1", "nonce": "n", "echostr": echostr}
    query["msg_signature"] = crypto.signature("1", "n", echostr)
    verify = await desk.client.get(hook_path(wecom_bot), params=query)
    assert (verify.status_code, verify.text) == (200, "hello")
    await desk.sql("UPDATE staff SET wecom_userid = 'zhangsan' WHERE id = $1", mei.staff_id)
    plaintext = json.dumps(
        {
            "msgtype": "text",
            "text": {"content": "我的待办"},
            "from": {"userid": "zhangsan"},
            "chattype": "single",
            "msgid": "wm1",
            "create_time": int(time.time()),
            "response_url": "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?code=x",
        },
        ensure_ascii=False,
    )
    encrypted = crypto.encrypt(plaintext, "corp")
    posted = await desk.client.post(
        hook_path(wecom_bot),
        params={
            "timestamp": "2",
            "nonce": "n",
            "msg_signature": crypto.signature("2", "n", encrypted),
        },
        json={"encrypt": encrypted},
    )
    assert posted.status_code == 200
    await desk.flush()
    assert "回访张总" in fake_bots.texts("wecom")[-1]
    tampered = await desk.client.post(
        hook_path(wecom_bot),
        params={"timestamp": "2", "nonce": "n", "msg_signature": "bad"},
        json={"encrypt": encrypted},
    )
    assert tampered.status_code == 403
    identities = await call(desk, desk.admin, "GET", "/api/v1/assistant/identities")
    wecom_identity = next(i for i in identities["items"] if i["provider"] == "wecom")
    assert (wecom_identity["external_user_id"], wecom_identity["staff_name"]) == ("zhangsan", "Mei")
    # 企业微信机器人不能主动发消息：测试发送报错说明。
    not_sent = await call(
        desk, desk.admin, "POST", f"/api/v1/assistant/bots/{wecom_bot['id']}/test"
    )
    assert not_sent["ok"] is False and "不能主动发消息" in not_sent["error"]


# ---- 机器人管理 ----


async def test_bot_management_and_manual_binding(desk: Desk, fake_bots: FakeBots) -> None:
    bot = await add_telegram(desk)
    mei = await desk.agent("mei", roles=["agent"], online=False)
    wang = await desk.agent("wang", roles=["worker"], online=False)
    await telegram_says(desk, bot, 1001, "你好")
    # 管理员手工把出现过的账号对应到员工、解除。
    [identity] = (await call(desk, desk.admin, "GET", "/api/v1/assistant/identities"))["items"]
    assert identity["staff_id"] is None
    bound = await call(
        desk,
        desk.admin,
        "PUT",
        f"/api/v1/assistant/identities/{identity['id']}",
        staff_id=str(mei.staff_id),
    )
    assert bound["staff_name"] == "Mei"
    await call(
        desk,
        desk.admin,
        "PUT",
        f"/api/v1/assistant/identities/{identity['id']}",
        422,
        staff_id=str(uuid.uuid4()),
    )
    # 同一个员工换账号：旧账号解除。
    await telegram_says(desk, bot, 1002, "你好", name="Mei2")
    second = next(
        i
        for i in (await call(desk, desk.admin, "GET", "/api/v1/assistant/identities"))["items"]
        if i["external_user_id"] == "1002"
    )
    await call(
        desk,
        desk.admin,
        "PUT",
        f"/api/v1/assistant/identities/{second['id']}",
        staff_id=str(mei.staff_id),
    )
    identities = {
        i["external_user_id"]: i["staff_id"]
        for i in (await call(desk, desk.admin, "GET", "/api/v1/assistant/identities"))["items"]
    }
    assert identities == {"1001": None, "1002": str(mei.staff_id)}
    await call(desk, desk.admin, "DELETE", f"/api/v1/assistant/identities/{second['id']}", 204)
    # 工人不能管理。
    await call(desk, wang.headers, "GET", "/api/v1/assistant/bots", 403)

    # 修改：不填密钥时保持原密钥；停用后回调被拒绝；换令牌后旧地址失效。
    renamed = await call(
        desk,
        desk.admin,
        "PUT",
        f"/api/v1/assistant/bots/{bot['id']}",
        provider="telegram",
        name="助理二号",
        config={},
        secrets={},
    )
    assert (renamed["name"], renamed["secret_keys"]) == ("助理二号", ["bot_token"])
    await call(
        desk,
        desk.admin,
        "PUT",
        f"/api/v1/assistant/bots/{bot['id']}",
        422,
        provider="feishu",
        name="x",
    )
    disabled = await call(desk, desk.admin, "POST", f"/api/v1/assistant/bots/{bot['id']}/disable")
    assert disabled["status"] == "disabled"
    assert (await telegram_says(desk, bot, 1001, "你好")).status_code == 403
    await call(desk, desk.admin, "POST", f"/api/v1/assistant/bots/{bot['id']}/enable")
    rotated = await call(
        desk, desk.admin, "POST", f"/api/v1/assistant/bots/{bot['id']}/rotate-token"
    )
    assert rotated["webhook_url"] != bot["webhook_url"]
    assert fake_bots.webhooks[-1]["url"] == rotated["webhook_url"]
    assert (await telegram_says(desk, bot, 1001, "你好")).status_code == 404
    assert (await telegram_says(desk, rotated, 1001, "你好")).status_code == 200
    # 审计留痕；删除后账号一起删除。
    actions = await desk.sql(
        "SELECT action FROM audit_logs WHERE action LIKE 'assistant.%' ORDER BY created_at"
    )
    assert {r["action"] for r in actions} >= {
        "assistant.bot.create", "assistant.bot.update", "assistant.bot.disable",
        "assistant.bot.enable", "assistant.bot.rotate", "assistant.identity.bind",
        "assistant.identity.unbind",
    }  # fmt: skip
    await call(desk, desk.admin, "DELETE", f"/api/v1/assistant/bots/{bot['id']}", 204)
    assert (await call(desk, desk.admin, "GET", "/api/v1/assistant/identities"))["items"] == []
    assert (await call(desk, desk.admin, "GET", "/api/v1/assistant/bots"))["items"] == []
