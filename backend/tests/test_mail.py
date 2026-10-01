"""邮件渠道（设计文档 §10.8）：添加邮箱、收信进会话（直接交给客服、未读）、回复、过滤、故障。

邮箱服务器用 tests/fake_mail.py（IMAP、SMTP，不加密，测试里放开内网地址）。
"""

import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.core.errors import Unprocessable
from app.integrations import mail as transport
from app.modules.conversation.outbox import dispatch_due
from app.modules.customer.sensitive import email_index
from app.modules.files.service import key_of_url
from app.modules.mail import inbox, original, parse, providers, service
from app.modules.mail.schemas import MailAccountIn, MailServer
from app.modules.sessions.engine import run_session_timers
from tests.desk import Agent, Desk
from tests.fake_mail import FakeMail, compose
from tests.fake_openim import ADMIN_USER_ID, FakeOpenIM
from tests.fake_storage import FakeStorage
from tests.support import DatabaseUrls
from tests.test_ai_reception import enable_ai

ADDRESS = "support@acme.test"
SECRET = "auth-code-0163"
CUSTOMER = "wang@customer.test"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    # 模拟的邮箱服务器在本机、不加密。
    return settings.model_copy(update={"mail_allow_private_hosts": True})


@pytest.fixture
async def fake_mail() -> AsyncIterator[FakeMail]:
    server = FakeMail()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


def mailbox(fake: FakeMail, **changes: Any) -> dict[str, Any]:
    """添加邮箱的请求：网易 163 的类型，服务器换成模拟的。"""
    return {
        "name": "售后邮箱",
        "address": ADDRESS,
        "display_name": "Acme 客服",
        "provider": "netease163",
        "secret": SECRET,
        "imap": {"host": "127.0.0.1", "port": fake.imap_port, "security": "none"},
        "smtp": {"host": "127.0.0.1", "port": fake.smtp_port, "security": "none"},
        "signature": "Acme 客服部\n电话 400-000-0000",
        **changes,
    }


async def add(desk: Desk, fake: FakeMail, **changes: Any) -> dict[str, Any]:
    if ADDRESS not in fake.mailboxes:
        fake.add_mailbox(ADDRESS, SECRET, require_id=True)
    response = await desk.client.post(
        "/api/v1/mail/accounts", headers=desk.admin, json=mailbox(fake, **changes)
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def receive(desk: Desk, account: dict[str, Any]) -> inbox.FetchResult:
    """收一次信，再让消息流动起来（归入会话、分配）。"""
    result = await inbox.fetch(desk.ctx, desk.tenant_id, uuid.UUID(account["id"]))
    await desk.flush()
    return result


def order_mail(**changes: Any) -> bytes:
    fields: dict[str, Any] = {
        "sender": CUSTOMER,
        "sender_name": "王小明",
        "to": ADDRESS,
        "subject": "订购 100 个保温杯",
        "text": "你好，\n我们公司想订 100 个保温杯，印 logo，月底前要。\n\n"
        "在 2026年9月30日 10:00，Acme 客服 写道：\n> 您好，有什么可以帮您？",
        "html": '<p>你好，</p><p>我们公司想订 100 个保温杯。</p><img src="cid:logo1">',
        "inline_images": [("logo1", "image/png", PNG)],
        "attachments": [("需求清单.xlsx", XLSX, b"PK\x03\x04 order list")],
        "message_id": "<order-1@customer.test>",
    }
    return compose(**{**fields, **changes})


async def sessions(desk: Desk, headers: dict[str, str]) -> list[dict[str, Any]]:
    response = await desk.client.get("/api/v1/sessions", headers=headers, params={"status": "open"})
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def send(desk: Desk, agent: Agent, session_id: Any, **body: Any) -> httpx.Response:
    return await desk.client.post(
        f"/api/v1/sessions/{session_id}/messages",
        headers=agent.headers,
        json={"client_msg_id": uuid.uuid4().hex, "type": "text", **body},
    )


async def incoming(desk: Desk, fake: FakeMail) -> tuple[Agent, dict[str, Any], Any]:
    """在线的坐席 Amy、一个邮箱，客户发来一封订购邮件。"""
    amy = await desk.agent("amy")
    account = await add(desk, fake)
    fake.deliver(ADDRESS, order_mail())
    result = await receive(desk, account)
    assert (result.imported, result.error) == (1, None)
    [chat] = await desk.sql("SELECT * FROM sessions")
    return amy, account, chat


# ---- 添加邮箱 ----


async def test_adding_a_mailbox_tests_the_connection_and_skips_old_mail(
    desk: Desk, fake_mail: FakeMail
) -> None:
    box = fake_mail.add_mailbox(ADDRESS, SECRET, require_id=True)
    fake_mail.deliver(ADDRESS, compose(sender=CUSTOMER, to=ADDRESS, subject="以前的", text="旧"))

    wrong = await desk.client.post(
        "/api/v1/mail/accounts/test", headers=desk.admin, json=mailbox(fake_mail, secret="x")
    )
    assert wrong.status_code == 200, wrong.text
    assert wrong.json()["ok"] is False
    assert "邮箱拒绝登录" in wrong.json()["imap_error"]
    assert "邮箱拒绝登录发信服务器" in wrong.json()["smtp_error"]
    refused = await desk.client.post(
        "/api/v1/mail/accounts", headers=desk.admin, json=mailbox(fake_mail, secret="x")
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["message"].startswith("连接邮箱失败：收信：邮箱拒绝登录")
    tested = await desk.client.post(
        "/api/v1/mail/accounts/test", headers=desk.admin, json=mailbox(fake_mail)
    )
    assert tested.json() == {"ok": True, "imap_error": None, "smtp_error": None, "inbox": 1}

    account = await add(desk, fake_mail)
    assert (account["status"], account["provider"], account["username"]) == (
        "active",
        "netease163",
        ADDRESS,
    )
    assert SECRET not in json.dumps(account)
    [row] = await desk.sql("SELECT * FROM mail_accounts")
    assert SECRET not in row["secret_enc"]
    # 只记下当前位置：添加之前的邮件不导入。
    assert (row["uidvalidity"], row["last_uid"]) == (box.uidvalidity, 1)
    [channel] = await desk.sql(
        "SELECT type, name, status FROM channel_accounts WHERE id = $1",
        uuid.UUID(account["channel_account_id"]),
    )
    assert tuple(channel) == ("email", "售后邮箱", "active")
    duplicate = await desk.client.post(
        "/api/v1/mail/accounts",
        headers=desk.admin,
        json=mailbox(fake_mail, address=ADDRESS.upper()),
    )
    assert duplicate.status_code == 409

    fetched = await desk.client.post(
        f"/api/v1/mail/accounts/{account['id']}/fetch", headers=desk.admin
    )
    assert fetched.status_code == 200, fetched.text
    assert (fetched.json()["imported"], fetched.json()["error"]) == (0, None)
    assert await desk.sql("SELECT id FROM messages") == []
    # 网易邮箱要求先发 ID；只读打开收件箱，不改变邮件的已读状态。
    assert box.logins >= 3 and box.seen == set()
    audit = await desk.sql("SELECT action FROM audit_logs WHERE action LIKE 'mail.%'")
    assert [a["action"] for a in audit] == ["mail.create"]
    listed = await desk.client.get("/api/v1/mail/accounts", headers=desk.admin)
    assert [a["address"] for a in listed.json()["items"]] == [ADDRESS]


async def test_providers_presets_and_validation(desk: Desk, settings: Settings) -> None:
    response = await desk.client.get("/api/v1/mail/providers", headers=desk.admin)
    assert response.status_code == 200, response.text
    found = {p["key"]: p for p in response.json()["items"]}
    assert {"netease163", "netease126", "qq", "exmail", "qiye163", "aliyun", "gmail"} <= set(found)
    assert found["netease163"]["imap"] == {"host": "imap.163.com", "port": 993, "security": "ssl"}
    assert found["qq"]["smtp"] == {"host": "smtp.qq.com", "port": 465, "security": "ssl"}
    assert found["gmail"]["secret_label"] == "应用专用密码"
    assert providers.guess("li@foxmail.com").key == "qq"
    assert providers.guess("li@126.com").key == "netease126"
    assert providers.guess("li@company.test").key == "custom"

    strict = settings.model_copy(update={"mail_allow_private_hosts": False})
    preset = service._validate(
        strict, MailAccountIn(name="售后", address=" Li@163.com ", provider="netease163")
    )
    assert (preset.address, preset.imap.host, preset.smtp.port) == (
        "li@163.com",
        "imap.163.com",
        465,
    )
    plain = MailServer(host="mail.company.test", port=143, security="none")
    with pytest.raises(Unprocessable, match="加密连接"):
        service._validate(
            strict, MailAccountIn(name="x", address="a@company.test", provider="custom", imap=plain)
        )
    with pytest.raises(Unprocessable, match="忽略的发件人"):
        service._validate(
            strict,
            MailAccountIn(
                name="x", address="a@163.com", provider="netease163", ignore_senders=["spam"]
            ),
        )
    # 不能借邮箱设置连接内网或本机地址，端口只能是常用端口。
    for host, port in (
        ("127.0.0.1", 993),
        ("localhost", 993),
        ("10.1.2.3", 993),
        ("169.254.169.254", 993),
        ("::1", 993),
        ("imap.163.com", 8080),
    ):
        with pytest.raises(transport.MailError):
            transport._resolve(host, port, transport.IMAP_PORTS, allow_private=False)


async def test_mail_settings_need_the_settings_permission(desk: Desk, fake_mail: FakeMail) -> None:
    amy = await desk.agent("amy")
    for method, path in (
        ("GET", "/api/v1/mail/providers"),
        ("GET", "/api/v1/mail/accounts"),
        ("POST", "/api/v1/mail/accounts/test"),
    ):
        response = await desk.client.request(
            method, path, headers=amy.headers, json=mailbox(fake_mail)
        )
        assert response.status_code == 403, path


# ---- 收信：直接交给客服，进来就是未读 ----


async def test_new_mail_becomes_an_unread_session_for_a_human_agent(
    desk: Desk, fake_mail: FakeMail
) -> None:
    # 路由策略是 AI 优先：邮件也不经过 AI。
    await enable_ai(desk)
    amy, _, chat = await incoming(desk, fake_mail)

    assert (chat["status"], chat["assignee_id"]) == ("human_serving", amy.staff_id)
    events = await desk.events_of(chat["id"])
    assert "ai_serving" not in events and events[:2] == ["created", "queued"]
    [queued] = await desk.sql(
        "SELECT payload FROM session_events WHERE session_id = $1 AND type = 'queued'", chat["id"]
    )
    assert json.loads(queued["payload"])["reason"] == "email"
    assert await desk.sql("SELECT id FROM ai_decisions") == []

    [customer] = await desk.sql("SELECT * FROM customers")
    assert (customer["display_name"], customer["source_channel"]) == ("王小明", "email")
    key = await email_index(desk.ctx.keys, desk.tenant_id, CUSTOMER)
    assert customer["email_hash"] == key and CUSTOMER not in customer["email_enc"]
    [identity] = await desk.sql("SELECT * FROM customer_identities")
    assert identity["external_id"] == key
    profile = json.loads(identity["profile"])
    assert profile["name"] == "王小明" and CUSTOMER not in json.dumps(profile)

    rows = await desk.sql(
        "SELECT id, content_type, content, text_plain, session_id FROM messages ORDER BY sent_at"
    )
    assert [r["content_type"] for r in rows] == ["email", "file"]
    email, attachment = rows
    content = json.loads(email["content"])
    assert content["subject"] == "订购 100 个保温杯"
    assert content["from"] == {"name": "王小明", "address": CUSTOMER}
    assert content["text"] == "你好，\n我们公司想订 100 个保温杯，印 logo，月底前要。"
    assert content["quoted"].startswith("在 2026年9月30日 10:00，Acme 客服 写道：")
    assert content["message_id"] == "<order-1@customer.test>"
    assert content["attachments"] == 1 and key_of_url(desk.settings, content["url"])
    assert email["text_plain"].startswith("订购 100 个保温杯\n你好，")
    file = json.loads(attachment["content"])
    # 正文里引用的内嵌图片不单独列出。
    assert (file["name"], file["email_id"]) == ("需求清单.xlsx", str(email["id"]))
    assert {r["session_id"] for r in rows} == {chat["id"]}

    # 服务群里只有客户的消息：没有排队、接待提示，也没有 AI 回复。
    [room] = await desk.sql("SELECT im_group_id FROM rooms")
    group = desk.im.groups[room["im_group_id"]]
    posted = [m for m in group.messages if m.send_id != ADMIN_USER_ID]  # 群通知除外
    assert {m.send_id for m in posted} == {identity["im_user_id"]}
    assert json.loads(posted[0].content)["content"].startswith("[邮件] 订购 100 个保温杯")
    assert fake_mail.mailboxes[ADDRESS].seen == set()

    # 客户面板：邮件身份只返回发件人名称，加密保存的地址不返回。
    detail = await desk.client.get(f"/api/v1/customers/{customer['id']}", headers=amy.headers)
    [shown_identity] = detail.json()["identities"]
    assert shown_identity["channel_type"] == "email"
    assert shown_identity["profile"] == {"name": "王小明"}

    [item] = await sessions(desk, amy.headers)
    assert (item["channel_type"], item["unread"], item["email_subject"]) == (
        "email",
        2,
        "订购 100 个保温杯",
    )
    # 只对接待坐席算未读。
    [seen_by_admin] = await sessions(desk, desk.admin)
    assert seen_by_admin["unread"] == 0
    read = await desk.client.post(f"/api/v1/sessions/{chat['id']}/read", headers=amy.headers)
    assert read.status_code == 204
    [item] = await sessions(desk, amy.headers)
    assert item["unread"] == 0

    shown = await desk.client.get(
        f"/api/v1/mail/messages/{email['id']}/original", headers=amy.headers
    )
    assert shown.status_code == 200, shown.text
    html = shown.json()["html"]
    assert shown.json()["subject"] == "订购 100 个保温杯"
    assert "Content-Security-Policy" in html and "data:image/png;base64," in html
    assert "我们公司想订 100 个保温杯" in html
    bob = await desk.agent("bob")
    hidden = await desk.client.get(
        f"/api/v1/mail/messages/{email['id']}/original", headers=bob.headers
    )
    assert hidden.status_code == 404


async def test_agent_replies_by_email_in_the_same_thread(
    desk: Desk, fake_mail: FakeMail, fake_storage: FakeStorage
) -> None:
    amy, account, chat = await incoming(desk, fake_mail)

    response = await send(
        desk, amy, chat["id"], text="王先生您好：\n100 个保温杯可以做，报价见下一封邮件。"
    )
    assert response.status_code == 200, response.text
    sent = response.json()
    assert (sent["send_status"], sent["content_type"]) == ("sent", "email")
    assert sent["content"]["subject"] == "Re: 订购 100 个保温杯"
    [mail] = fake_mail.sent
    summary = mail.summary()
    assert (summary["mail_from"], summary["rcpt_to"]) == (ADDRESS, [CUSTOMER])
    assert summary["subject"] == "Re: 订购 100 个保温杯"
    assert "Acme 客服" in summary["from"] and summary["to"].endswith(f"<{CUSTOMER}>")
    assert summary["in_reply_to"] == "<order-1@customer.test>"
    assert summary["references"] == "<order-1@customer.test>"
    text = summary["text"].replace("\r\n", "\n")
    assert text.startswith("王先生您好：\n100 个保温杯可以做")
    assert "--\nAcme 客服部\n电话 400-000-0000" in text
    assert "王小明 <wang@customer.test> 写道：\n> 你好，" in text
    [row] = await desk.sql("SELECT ext_msg_id, send_status FROM messages WHERE direction = 'out'")
    assert (row["ext_msg_id"], row["send_status"]) == (summary["message_id"], "sent")
    # 服务群里也有这条回复（镜像）。
    [room] = await desk.sql("SELECT im_group_id FROM rooms")
    assert any(
        json.loads(m.content).get("content", "").startswith("[邮件] Re: 订购 100 个保温杯")
        for m in desk.im.groups[room["im_group_id"]].messages
    )

    # 文件：一封带附件的邮件。
    upload = await desk.client.post(
        "/api/v1/uploads",
        headers=amy.headers,
        json={"filename": "报价单.pdf", "content_type": "application/pdf", "size": 9},
    )
    file_url = upload.json()["file_url"]
    key = key_of_url(desk.settings, file_url)
    fake_storage.objects[f"/edp-files/{key}"] = (b"%PDF-1.4\n", "application/pdf")
    attached = await send(
        desk,
        amy,
        chat["id"],
        type="file",
        text=None,
        attachment={
            "url": file_url,
            "name": "报价单.pdf",
            "size": 9,
            "content_type": "application/pdf",
        },
    )
    assert attached.status_code == 200, attached.text
    assert attached.json()["content_type"] == "file"
    second = fake_mail.sent[-1]
    assert second.attachments() == [("报价单.pdf", b"%PDF-1.4\n")]
    assert second.summary()["subject"] == "Re: 订购 100 个保温杯"
    assert "请查收附件：报价单.pdf" in second.text()

    # 客户在同一个邮件会话里回信（带 Reply-To）：归到进行中的会话，对 Amy 是未读。
    fake_mail.deliver(
        ADDRESS,
        compose(
            sender=CUSTOMER,
            sender_name="王小明",
            to=ADDRESS,
            subject="Re: 订购 100 个保温杯",
            text="好的，就按这个价格，发票开给采购部。",
            in_reply_to=summary["message_id"],
            references=f"<order-1@customer.test> {summary['message_id']}",
            headers={"Reply-To": "采购部 <purchase@customer.test>"},
        ),
    )
    assert (await receive(desk, account)).imported == 1
    assert len(await desk.sql("SELECT id FROM sessions")) == 1
    [item] = await sessions(desk, amy.headers)
    assert item["unread"] == 1 and item["email_subject"] == "Re: 订购 100 个保温杯"
    # 默认回复最近的一封：发到它的 Reply-To。
    response = await send(desk, amy, chat["id"], text="收到，发票会开给采购部。")
    assert response.status_code == 200, response.text
    latest = fake_mail.sent[-1].summary()
    assert latest["rcpt_to"] == ["purchase@customer.test"]
    assert latest["subject"] == "Re: 订购 100 个保温杯"
    assert latest["references"].split() == ["<order-1@customer.test>", summary["message_id"]] + [
        r for r in latest["references"].split()[2:]
    ]
    [item] = await sessions(desk, amy.headers)
    assert item["unread"] == 0
    # 指定回复第一封邮件，主题可以改。
    [first] = await desk.sql(
        "SELECT id FROM messages WHERE content_type = 'email' AND direction = 'in'"
        " ORDER BY sent_at LIMIT 1"
    )
    response = await send(
        desk, amy, chat["id"], text="补充一下交期。", reply_to=str(first["id"]), subject="交期说明"
    )
    assert response.status_code == 200, response.text
    custom = fake_mail.sent[-1].summary()
    assert (custom["rcpt_to"], custom["subject"]) == ([CUSTOMER], "交期说明")
    assert custom["in_reply_to"] == "<order-1@customer.test>"


async def test_reply_failures_are_shown_and_network_errors_are_retried(
    desk: Desk, fake_mail: FakeMail
) -> None:
    amy, account, chat = await incoming(desk, fake_mail)

    fake_mail.reject_recipients.add(CUSTOMER)
    message_id = uuid.uuid4().hex
    body = {"client_msg_id": message_id, "type": "text", "text": "您好"}
    failed = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/messages", headers=amy.headers, json=body
    )
    assert failed.status_code == 409
    assert failed.json()["error"]["message"].startswith("发信失败：收件人地址被拒绝")
    fake_mail.reject_recipients.clear()
    retried = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/messages", headers=amy.headers, json=body
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["send_status"] == "sent" and len(fake_mail.sent) == 1

    # 发信服务器暂时不可用：保持"发送中"，发件箱稍后重试。
    fake_mail.temporary_failures = 1
    pending = await send(desk, amy, chat["id"], text="稍后再试")
    assert pending.status_code == 200, pending.text
    assert pending.json()["send_status"] == "pending"
    await desk.sql("UPDATE im_ops SET next_attempt_at = now() WHERE status = 'pending'")
    await dispatch_due(desk.ctx)
    [row] = await desk.sql(
        "SELECT send_status FROM messages WHERE id = $1", uuid.UUID(pending.json()["id"])
    )
    assert row["send_status"] == "sent" and len(fake_mail.sent) == 2

    # 停用邮箱后不能回复，也不再收信。
    disabled = await desk.client.post(
        f"/api/v1/mail/accounts/{account['id']}/disable", headers=desk.admin
    )
    assert disabled.json()["status"] == "disabled"
    blocked = await send(desk, amy, chat["id"], text="还能发吗")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["message"] == "邮箱已停用，不能回复"


async def test_email_sessions_stay_with_humans(desk: Desk, fake_mail: FakeMail) -> None:
    await enable_ai(desk)
    amy, _, chat = await incoming(desk, fake_mail)

    returned = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/return-to-ai", headers=amy.headers
    )
    assert returned.status_code == 409
    assert returned.json()["error"]["message"] == "邮件会话由客服回复，不能交还 AI"

    # 订单通知不自动发邮件：提示客服在会话里回复。
    product = await desk.client.post(
        "/api/v1/products",
        headers=desk.admin,
        json={"code": "CUP-500", "name": "保温杯", "retail_price": "35"},
    )
    assert product.status_code == 201, product.text
    order = await desk.client.post(
        "/api/v1/orders",
        headers=desk.admin,
        json={
            "customer_id": str(chat["customer_id"]),
            "session_id": str(chat["id"]),
            "items": [{"product_id": product.json()["id"], "quantity": 100}],
            "receiver": {"name": "王小明", "phone": "13800001111", "address": "上海市浦东新区"},
        },
    )
    assert order.status_code == 201, order.text
    confirmed = await desk.client.post(
        f"/api/v1/orders/{order.json()['id']}/confirm",
        headers=desk.admin,
        json={"payment_method": "cod"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["notice"] == {
        "status": "manual",
        "channel": "email",
        "reason": "邮件客户请在会话里回复邮件告知",
    }
    await desk.flush()
    assert fake_mail.sent == []


async def test_auto_replies_bounces_bulk_and_ignored_senders_are_not_imported(
    desk: Desk, fake_mail: FakeMail
) -> None:
    account = await add(desk, fake_mail, ignore_senders=["@spam.test", "Boss@Partner.test"])

    def mail(sender: str, **changes: Any) -> None:
        fake_mail.deliver(
            ADDRESS, compose(sender=sender, to=ADDRESS, subject="主题", text="内容", **changes)
        )

    mail(CUSTOMER, headers={"Auto-Submitted": "auto-replied"})
    mail(CUSTOMER, headers={"X-Autoreply": "yes"})
    mail("MAILER-DAEMON@mx.customer.test")
    mail("news@news.test", headers={"List-Unsubscribe": "<mailto:off@news.test>"})
    mail("promo@shop.test", headers={"Precedence": "bulk"})
    mail("noreply@shop.test")
    mail(ADDRESS)
    mail("sales@spam.test")
    mail("boss@partner.test")
    mail(
        CUSTOMER,
        attachments=[("现场照片.jpg", "image/jpeg", JPEG), ("假图.png", "image/png", b"x")],
    )

    result = await receive(desk, account)

    assert (result.imported, result.ignored) == (1, 9)
    rows = await desk.sql("SELECT content_type, content FROM messages ORDER BY sent_at")
    assert [r["content_type"] for r in rows] == ["email", "image", "file"]
    # 内容不是图片却用图片的扩展名：作为文件，改个扩展名。
    assert json.loads(rows[2]["content"])["name"] == "假图.png.bin"
    shown = await desk.client.get(f"/api/v1/mail/accounts/{account['id']}", headers=desk.admin)
    assert shown.json()["ignored"] == 9


async def test_large_mails_and_attachments_are_not_imported(
    desk: Desk, fake_mail: FakeMail, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await add(desk, fake_mail)
    monkeypatch.setattr(desk.ctx.settings, "mail_max_attachment_bytes", 1000)
    fake_mail.deliver(
        ADDRESS,
        compose(
            sender=CUSTOMER,
            to=ADDRESS,
            subject="图纸",
            text="见附件",
            attachments=[
                ("图纸.dwg", "application/acad", b"0" * 5000),
                ("说明.txt", "text/plain", b"ok"),
            ],
        ),
    )
    assert (await receive(desk, account)).imported == 1
    [email, note] = await desk.sql("SELECT content_type, content FROM messages ORDER BY sent_at")
    assert note["content_type"] == "file"
    assert "附件超过大小上限，没有导入：图纸.dwg（0.0 MB）" in json.loads(email["content"])["text"]

    monkeypatch.setattr(desk.ctx.settings, "mail_max_message_bytes", 2000)
    fake_mail.deliver(
        ADDRESS,
        compose(
            sender=CUSTOMER,
            to=ADDRESS,
            subject="大附件",
            text="见附件",
            attachments=[("视频.mp4", "video/mp4", b"0" * 5000)],
        ),
    )
    assert (await receive(desk, account)).imported == 1
    [big] = await desk.sql("SELECT content FROM messages WHERE content->>'subject' = '大附件'")
    content = json.loads(big["content"])
    assert "没有导入正文和附件，请到邮箱中查看" in content["text"] and content["url"] is None


async def test_email_sessions_ignore_business_hours_wait_for_agents_and_stay_open_until_answered(
    desk: Desk, fake_mail: FakeMail
) -> None:
    today = str(datetime.now(UTC).isoweekday())
    tomorrow = str(datetime.now(UTC).isoweekday() % 7 + 1)
    assert today != tomorrow
    # AI 优先、今天全天休息、没有在线客服。
    await enable_ai(desk, business_hours={"tz": "UTC", "days": {tomorrow: [["00:00", "24:00"]]}})
    account = await add(desk, fake_mail)
    fake_mail.deliver(ADDRESS, order_mail(subject="周末能发货吗", html=None, inline_images=None))
    await receive(desk, account)

    [chat] = await desk.sql("SELECT * FROM sessions")
    assert chat["status"] == "queued"
    assert await desk.sql("SELECT id FROM todos") == []
    # 不会排队超时转成留言。
    await run_session_timers(desk.ctx, now=datetime.now(UTC) + timedelta(hours=5))
    await desk.flush()
    [chat] = await desk.sql("SELECT * FROM sessions")
    assert chat["status"] == "queued"

    amy = await desk.agent("amy")
    [chat] = await desk.sql("SELECT * FROM sessions")
    assert (chat["status"], chat["assignee_id"]) == ("human_serving", amy.staff_id)
    [item] = await sessions(desk, amy.headers)
    assert item["unread"] == 2

    # 客户的邮件还没回复：再久也不自动结束。
    await desk.sql("UPDATE agent_states SET last_seen_at = now() + interval '30 days'")
    await run_session_timers(desk.ctx, now=datetime.now(UTC) + timedelta(days=3))
    [chat] = await desk.sql("SELECT * FROM sessions")
    assert chat["status"] == "human_serving"
    assert (await send(desk, amy, chat["id"], text="周末也发货。")).status_code == 200
    [chat] = await desk.sql("SELECT * FROM sessions")
    await run_session_timers(desk.ctx, now=chat["last_agent_message_at"] + timedelta(minutes=31))
    await desk.flush()
    [chat] = await desk.sql("SELECT * FROM sessions")
    assert (chat["status"], chat["close_reason"]) == ("closed", "idle_timeout")
    # 只发了 Amy 的那封回复：分配提示、结束提示都不发给客户。
    assert [m.summary()["text"].split("\r\n")[0] for m in fake_mail.sent] == ["周末也发货。"]


# ---- 故障 ----


async def test_login_failures_pause_the_mailbox_and_notify_admins(
    desk: Desk, fake_mail: FakeMail
) -> None:
    account = await add(desk, fake_mail)
    box = fake_mail.mailboxes[ADDRESS]
    box.secret = "changed-in-163"
    for _ in range(3):
        result = await inbox.fetch(desk.ctx, desk.tenant_id, uuid.UUID(account["id"]))
        assert result.auth and result.error and "邮箱拒绝登录" in result.error

    [row] = await desk.sql("SELECT status, failures, last_error FROM mail_accounts")
    assert (row["status"], row["failures"]) == ("paused", 3)
    [note] = await desk.sql(
        "SELECT title, link FROM staff_notifications WHERE kind = 'mail_paused'"
    )
    assert note["title"] == f"邮箱 {ADDRESS} 已暂停收信" and note["link"] == "/settings?tab=mail"
    logins = box.logins
    assert await inbox.poll_due(desk.ctx, now=datetime.now(UTC) + timedelta(hours=2)) == 0
    assert box.logins == logins

    box.secret = "new-code"
    fixed = await desk.client.put(
        f"/api/v1/mail/accounts/{account['id']}",
        headers=desk.admin,
        json=mailbox(fake_mail, secret="new-code"),
    )
    assert fixed.status_code == 200, fixed.text
    assert (fixed.json()["status"], fixed.json()["failures"], fixed.json()["last_error"]) == (
        "active",
        0,
        None,
    )
    fake_mail.deliver(ADDRESS, order_mail())
    assert await inbox.poll_due(desk.ctx, now=datetime.now(UTC) + timedelta(minutes=1)) == 1


async def test_unreachable_server_backs_off(desk: Desk, fake_mail: FakeMail) -> None:
    account = await add(desk, fake_mail)
    await fake_mail.stop()
    before = datetime.now(UTC)

    result = await inbox.fetch(desk.ctx, desk.tenant_id, uuid.UUID(account["id"]))

    assert result.error and not result.auth
    [row] = await desk.sql("SELECT status, failures, last_error, next_poll_at FROM mail_accounts")
    assert (row["status"], row["failures"]) == ("active", 1)
    assert row["last_error"].startswith("连接收信服务器 127.0.0.1")
    assert timedelta(seconds=55) < row["next_poll_at"] - before < timedelta(seconds=70)


async def test_rebuilt_mailbox_restarts_from_the_current_position(
    desk: Desk, fake_mail: FakeMail
) -> None:
    account = await add(desk, fake_mail)
    fake_mail.deliver(ADDRESS, order_mail(message_id="<a@customer.test>"))
    fake_mail.reset_uidvalidity(ADDRESS)

    assert (await receive(desk, account)).imported == 0
    fake_mail.deliver(ADDRESS, order_mail(message_id="<b@customer.test>"))
    assert (await receive(desk, account)).imported == 1
    # 同一封邮件（Message-ID 相同）不重复导入。
    fake_mail.deliver(ADDRESS, order_mail(message_id="<b@customer.test>"))
    assert (await receive(desk, account)).imported == 0


async def test_disabling_the_email_channel_stops_the_mailbox(
    desk: Desk, fake_mail: FakeMail
) -> None:
    account = await add(desk, fake_mail)
    channel_id = account["channel_account_id"]
    box = fake_mail.mailboxes[ADDRESS]

    patched = await desk.client.patch(
        f"/api/v1/channels/{channel_id}", headers=desk.admin, json={"status": "disabled"}
    )
    assert patched.status_code == 200, patched.text
    [row] = await desk.sql("SELECT status FROM mail_accounts")
    assert row["status"] == "disabled"
    logins = box.logins
    assert await inbox.poll_due(desk.ctx, now=datetime.now(UTC) + timedelta(hours=1)) == 0
    assert box.logins == logins
    refused = await desk.client.post(
        f"/api/v1/mail/accounts/{account['id']}/fetch", headers=desk.admin
    )
    assert refused.status_code == 409

    await desk.client.patch(
        f"/api/v1/channels/{channel_id}", headers=desk.admin, json={"status": "active"}
    )
    [row] = await desk.sql("SELECT status FROM mail_accounts")
    assert row["status"] == "active"
    disabled = await desk.client.post(
        f"/api/v1/mail/accounts/{account['id']}/disable", headers=desk.admin
    )
    assert disabled.json()["status"] == "disabled"
    [channel] = await desk.sql(
        "SELECT status FROM channel_accounts WHERE id = $1", uuid.UUID(channel_id)
    )
    assert channel["status"] == "disabled"
    enabled = await desk.client.post(
        f"/api/v1/mail/accounts/{account['id']}/enable", headers=desk.admin
    )
    assert enabled.json()["status"] == "active"
    actions = await desk.sql(
        "SELECT action FROM audit_logs WHERE action LIKE 'mail.%' ORDER BY created_at"
    )
    assert [a["action"] for a in actions] == ["mail.create", "mail.disable", "mail.enable"]


async def test_original_falls_back_to_text_when_the_eml_is_gone(
    desk: Desk, fake_mail: FakeMail, fake_storage: FakeStorage
) -> None:
    amy, _, _ = await incoming(desk, fake_mail)
    [email] = await desk.sql("SELECT id, content FROM messages WHERE content_type = 'email'")
    key = key_of_url(desk.settings, json.loads(email["content"])["url"])
    fake_storage.objects.pop(f"/edp-files/{key}")

    shown = await desk.client.get(
        f"/api/v1/mail/messages/{email['id']}/original", headers=amy.headers
    )

    assert shown.status_code == 200, shown.text
    assert "我们公司想订 100 个保温杯，印 logo" in shown.json()["html"]


# ---- 网页会话的未读 ----


async def test_unread_counts_are_kept_on_the_server(desk: Desk) -> None:
    amy = await desk.agent("amy")
    bob = await desk.agent("bob")
    await desk.set_status(bob, "away")
    visitor = await desk.visitor()
    await desk.say(visitor, "在吗")
    await desk.say(visitor, "想问下发货时间")
    chat = await desk.session_of(visitor)
    assert chat["assignee_id"] == amy.staff_id

    [item] = await sessions(desk, amy.headers)
    assert (item["channel_type"], item["unread"], item["email_subject"]) == ("web", 2, None)
    # 别人查看不影响接待坐席的未读。
    assert (
        await desk.client.post(f"/api/v1/sessions/{chat['id']}/read", headers=desk.admin)
    ).status_code == 204
    [item] = await sessions(desk, amy.headers)
    assert item["unread"] == 2
    await desk.client.post(f"/api/v1/sessions/{chat['id']}/read", headers=amy.headers)
    await desk.say(visitor, "还在吗")
    detail = await desk.client.get(f"/api/v1/sessions/{chat['id']}", headers=amy.headers)
    assert detail.json()["unread"] == 1
    assert (await send(desk, amy, chat["id"], text="在的")).status_code == 200
    [item] = await sessions(desk, amy.headers)
    assert item["unread"] == 0

    # 转给 Bob：对 Bob 来说客户的消息都是未读。
    await desk.set_status(bob, "online")
    transfer = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/transfer",
        headers=amy.headers,
        json={"to_staff_id": str(bob.staff_id)},
    )
    assert transfer.status_code == 200, transfer.text
    accepted = await desk.client.post(
        f"/api/v1/transfers/{transfer.json()['id']}/accept", headers=bob.headers
    )
    assert accepted.status_code == 200, accepted.text
    [item] = await sessions(desk, bob.headers)
    assert item["unread"] == 3


# ---- 解析与显示 ----


def test_chinese_encodings_and_raw_headers_are_decoded() -> None:
    body = "订购 100 个保温杯，镕铸工艺".encode("gb18030")
    raw = (
        b"From: =?GB2312?B?"
        + base64.b64encode("王小明".encode("gbk"))
        + b"?= <wang@customer.test>\r\n"
        b"To: support@acme.test\r\n"
        b"Subject: " + "原始头部：订购".encode() + b"\r\n"
        b"Message-ID: <gbk-1@customer.test>\r\n"
        b'Content-Type: text/plain; charset="gb2312"\r\n'
        b"Content-Transfer-Encoding: 8bit\r\n\r\n" + body + b"\r\n"
    )
    parsed = parse.parse(raw)
    assert parsed.sender == parse.Address("王小明", "wang@customer.test")
    assert parsed.subject == "原始头部：订购"
    assert parsed.text == "订购 100 个保温杯，镕铸工艺"


@pytest.mark.parametrize(
    ("text", "new"),
    [
        (
            "好的，谢谢\n\n发件人: Acme <support@acme.test>\n发送时间: 2026年9月30日 10:00\n"
            "收件人: wang@customer.test\n主题: 报价\n\n原来的内容",
            "好的，谢谢",
        ),
        (
            "Sounds good.\n\nOn Tue, Sep 29, 2026 at 10:00 AM Acme <\n"
            "support@acme.test> wrote:\n> hi",
            "Sounds good.",
        ),
        ("收到\n------------------ 原始邮件 ------------------\n发件人: x", "收到"),
        ("行\n\n> 上一封\n> 的内容", "行"),
        ("> 全部是引用", "> 全部是引用"),
    ],
)
def test_quoted_history_is_split_off(text: str, new: str) -> None:
    assert parse.split_quoted(text)[0] == new


def test_html_only_mail_and_forwarded_mail() -> None:
    raw = compose(
        sender=CUSTOMER,
        to=ADDRESS,
        subject="Fwd: 询价",
        html="<div>需要：<ul><li>保温杯</li><li>马克杯</li></ul></div><script>x()</script>",
    )
    parsed = parse.parse(raw)
    assert parsed.html is True
    assert parsed.text == "需要：\n• 保温杯\n• 马克杯"
    assert parse.reply_subject(parsed.subject) == "Re: 询价"
    assert parse.reply_subject("回复：Re: 订购") == "Re: 订购"
    assert parse.reply_subject("") == "Re: 您的来信"


def test_original_html_is_sanitized_and_inline_images_shown() -> None:
    raw = compose(
        sender=CUSTOMER,
        to=ADDRESS,
        subject="样式",
        html=(
            '<meta http-equiv="refresh" content="0;url=https://evil.test">'
            '<p onclick="steal()">你好</p><script>steal()</script>'
            '<a href="javascript:steal()">点我</a><iframe src="https://evil.test"></iframe>'
            '<img src="https://tracker.test/p.gif"><img src="cid:logo1">'
        ),
        inline_images=[("logo1", "image/png", PNG)],
    )
    html = original.render(raw)
    assert "<script" not in html and "steal()" not in html.replace('href=""', "")
    assert "<iframe" not in html and "evil.test" not in html
    assert "onclick" not in html and "javascript:" not in html
    assert "default-src 'none'; img-src data:" in html
    assert "data:image/png;base64," in html and "你好" in html
    assert original.wrap_text("<b>粗体</b>").count("&lt;b&gt;") == 1
