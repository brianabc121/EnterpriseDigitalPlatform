"""微信客服（设计文档 §10.3）：每个客服账号是一个渠道，平台整体扮演"智能助手"。

入站
- 回调 kf_msg_or_event 只带一个 10 分钟有效的 token。实时消费进程按客服账号串行调用 sync_msg，
  从保存的游标开始拉取，每页连同游标一起提交；回调丢失时调度进程定时兜底拉取（消息只保留 3 天）。
- 平台开始管理某个客服账号之前的历史消息不导入。
- 按 msgid 去重（messages.ext_msg_id）。新客户用 kf/customer/batchget 取昵称、头像和 unionid；
  同一企业的外部联系人（客户联系）已有档案时归到同一位客户。图片、语音、视频、文件立即下载
  转存到对象存储（临时素材 3 天后失效）。
- 入库后发布 message.received（会话、路由、AI 接待与网页访客相同），并以客户身份镜像到服务群。
- 客户进入会话（enter_session）时，用事件响应消息发送客服账号的欢迎语（welcome_code 20 秒内
  有效、只能用一次）。
- 新接入的会话置为"由智能助手接待"（1）。之后 AI 和人工都在平台内接待，不转为"由人工接待"（3），
  否则平台就不能再发消息（设计 §3.1）。接待人员在企业微信客户端直接回复的消息（混合模式）
  只归档，不在平台内回复。

出站
- 客户最后一次发消息后 48 小时内最多发 5 条（reply_window）。坐席发送前检查一次，发件箱投递时
  再检查一次；客户再发消息后额度重置。欢迎语不占额度。
- AI 回复加"【AI】"前缀（纯文本渠道的 AI 生成内容标识，设计 §3.3）。
- 投递成功后再镜像到服务群；失败原因记在消息上，坐席工作台可见。
"""

import logging
import mimetypes
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.ids import new_id
from app.integrations.storage import StorageError
from app.integrations.wecom import WeComClient, WeComError, WeComUnavailable
from app.modules.channels.models import ChannelAccount, ChannelStatus, ChannelType
from app.modules.channels.service import new_public_key
from app.modules.conversation import imids, outbox
from app.modules.conversation.ingest import message_received
from app.modules.conversation.models import (
    ChatSession,
    Direction,
    Message,
    MessageSource,
    Room,
    SenderType,
    SendStatus,
)
from app.modules.customer.models import Customer, CustomerIdentity
from app.modules.files.service import file_url, key_of_url, safe_filename
from app.modules.iam.models import Staff
from app.modules.tenancy.models import Tenant
from app.modules.wecom.models import CorpStatus, KfAccountStatus, WecomCorp, WecomKfAccount
from app.modules.wecom.schemas import ReplyWindowOut
from app.modules.wecom.service import active_corp, client_of, record_sync

logger = logging.getLogger(__name__)

KF_WINDOW = timedelta(hours=48)
KF_WINDOW_LIMIT = 5
AI_LABEL = "【AI】"
TEXT_LIMIT_BYTES = 2048
DEFAULT_ACCOUNT_NAME = "微信客服"

ORIGIN_CUSTOMER = 3
ORIGIN_EVENT = 4
ORIGIN_SERVICER = 5
STATE_NEW = 0
STATE_BOT = 1

WINDOW_EXPIRED = "已超过 48 小时回复窗口，客户再次发消息后才能回复"
WINDOW_USED_UP = f"客户回复前最多只能发 {KF_WINDOW_LIMIT} 条消息，请等待客户回复"
NO_CUSTOMER_MESSAGE = "客户还没有发过消息，暂时不能回复"
ACCOUNT_UNAVAILABLE = "微信客服账号已停用或授权已取消"

_PAGE = 1000
_MAX_PAGES = 20
_HISTORY_GRACE = timedelta(minutes=10)
_SYNC_LOCK_TTL = 120
_STATE_TTL = 600
_MEDIA_LABELS = {"image": "图片", "voice": "语音", "video": "视频", "file": "文件"}
_FAIL_TYPES = {
    1: "客服账号已删除",
    2: "应用已关闭",
    4: "会话已过期（超过 48 小时）",
    5: "会话已关闭",
    6: "超过 5 条消息的限制",
    8: "企业主体未验证",
    10: "客户拒收消息",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---- 客服账号 ----


async def sync_accounts(ctx: AppContext, tenant_id: UUID) -> int:
    """同步交给本应用管理的客服账号：每个账号一个渠道，收回的账号停用渠道。返回账号数。"""
    wecom = client_of(ctx)
    async with ctx.db.tenant_session(tenant_id) as session:
        corp = await active_corp(session)
        tenant = await session.get(Tenant, tenant_id)
    if corp is None or tenant is None:
        return 0
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        data = await wecom.corp_call(
            corp.corp_id, "POST", "/cgi-bin/kf/account/list", json={"offset": offset, "limit": 100}
        )
        page = data.get("account_list") or []
        items += page
        if len(page) < 100:
            break
        offset += len(page)
    managed = {
        str(item["open_kfid"]): item
        for item in items
        if item.get("open_kfid") and item.get("manage_privilege", True)
    }
    async with ctx.db.tenant_session(tenant_id) as session:
        existing = {a.open_kfid: a for a in (await session.scalars(select(WecomKfAccount))).all()}
        for open_kfid, item in managed.items():
            name = str(item.get("name") or DEFAULT_ACCOUNT_NAME)
            account = existing.get(open_kfid)
            if account is None:
                channel = ChannelAccount(
                    id=new_id(),
                    tenant_id=tenant_id,
                    type=ChannelType.WECOM_KF,
                    name=name[:64],
                    public_key=new_public_key(tenant.code),
                    config={"kf": {"open_kfid": open_kfid}},
                )
                session.add(channel)
                await session.flush()
                session.add(
                    WecomKfAccount(
                        tenant_id=tenant_id,
                        corp_id=corp.corp_id,
                        open_kfid=open_kfid,
                        name=name[:128],
                        avatar=item.get("avatar"),
                        channel_account_id=channel.id,
                    )
                )
                continue
            account.name = name[:128]
            account.avatar = item.get("avatar")
            account.corp_id = corp.corp_id
            if account.status != KfAccountStatus.ACTIVE:
                account.status = KfAccountStatus.ACTIVE
                await session.execute(
                    update(ChannelAccount)
                    .where(ChannelAccount.id == account.channel_account_id)
                    .values(status=ChannelStatus.ACTIVE)
                )
        for open_kfid, account in existing.items():
            if open_kfid not in managed and account.status == KfAccountStatus.ACTIVE:
                account.status = KfAccountStatus.REMOVED
                await session.execute(
                    update(ChannelAccount)
                    .where(ChannelAccount.id == account.channel_account_id)
                    .values(status=ChannelStatus.DISABLED)
                )
        await session.commit()
        missing_links = (
            await session.scalars(
                select(WecomKfAccount.open_kfid).where(
                    WecomKfAccount.status == KfAccountStatus.ACTIVE,
                    WecomKfAccount.contact_url.is_(None),
                )
            )
        ).all()
    for open_kfid in missing_links:
        await _create_contact_url(ctx, wecom, tenant_id, corp.corp_id, open_kfid)
    await record_sync(ctx, tenant_id, "kf", count=len(managed))
    return len(managed)


async def _create_contact_url(
    ctx: AppContext, wecom: WeComClient, tenant_id: UUID, corp_id: str, open_kfid: str
) -> None:
    try:
        data = await wecom.corp_call(
            corp_id,
            "POST",
            "/cgi-bin/kf/add_contact_way",
            json={"open_kfid": open_kfid, "scene": "edp"},
        )
    except WeComError as exc:
        logger.warning("kf contact url for %s failed: %s", open_kfid, exc)
        return
    async with ctx.db.tenant_session(tenant_id) as session:
        await session.execute(
            update(WecomKfAccount)
            .where(WecomKfAccount.open_kfid == open_kfid)
            .values(contact_url=data.get("url"))
        )
        await session.commit()


# ---- 入站 ----


@dataclass
class _Batch:
    new_messages: list[tuple[UUID, UUID]] = field(default_factory=list)  # (room_id, message_id)
    rooms: set[UUID] = field(default_factory=set)
    customers: set[str] = field(default_factory=set)  # 发了消息的客户（检查会话状态）
    welcomes: list[tuple[UUID, str, str]] = field(default_factory=list)  # (room, code, text)
    reset_states: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _Account:
    id: UUID
    corp_id: str
    open_kfid: str
    channel_account_id: UUID
    cursor: str | None
    since: datetime


def _sync_lock(tenant_id: UUID, open_kfid: str) -> str:
    return f"edp:wecom:kfsync:{tenant_id}:{open_kfid}"


async def sync_messages(
    ctx: AppContext, tenant_id: UUID, open_kfid: str, *, token: str | None = None
) -> int:
    """拉取一个客服账号的新消息并入库，返回新入库的消息数。

    同一账号同时只有一个拉取者；拉取期间又来了回调时，拉取者结束后再拉一轮（带新的 token）。
    """
    wecom = client_of(ctx)
    lock = _sync_lock(tenant_id, open_kfid)
    if not await ctx.redis.set(lock, "1", nx=True, ex=_SYNC_LOCK_TTL):
        await ctx.redis.set(f"{lock}:again", token or "-", ex=_SYNC_LOCK_TTL)
        return 0
    total = 0
    try:
        while True:
            total += await _pull(ctx, wecom, tenant_id, open_kfid, token)
            again = await ctx.redis.getdel(f"{lock}:again")
            if not again:
                break
            value = again.decode() if isinstance(again, bytes) else str(again)
            token = None if value == "-" else value
    finally:
        await ctx.redis.delete(lock)
    return total


async def _pull(
    ctx: AppContext, wecom: WeComClient, tenant_id: UUID, open_kfid: str, token: str | None
) -> int:
    count = 0
    for _ in range(_MAX_PAGES):
        async with ctx.db.tenant_session(tenant_id) as session:
            row = await session.scalar(
                select(WecomKfAccount).where(
                    WecomKfAccount.open_kfid == open_kfid,
                    WecomKfAccount.status == KfAccountStatus.ACTIVE,
                )
            )
            tenant = await session.get(Tenant, tenant_id)
        if row is None or tenant is None:
            return count
        account = _Account(
            row.id, row.corp_id, row.open_kfid, row.channel_account_id, row.cursor, row.created_at
        )
        body: dict[str, Any] = {
            "cursor": account.cursor or "",
            "limit": _PAGE,
            "voice_format": 0,
            "open_kfid": open_kfid,
        }
        if token:
            body["token"] = token
        data = await wecom.corp_call(account.corp_id, "POST", "/cgi-bin/kf/sync_msg", json=body)
        count += await _ingest(
            ctx, wecom, tenant, account, data.get("msg_list") or [], data.get("next_cursor")
        )
        if not data.get("has_more"):
            break
    return count


async def _ingest(
    ctx: AppContext,
    wecom: WeComClient,
    tenant: Tenant,
    account: _Account,
    msgs: list[dict[str, Any]],
    next_cursor: str | None,
) -> int:
    # 授权前的历史消息不导入；授权前几分钟内发来的消息照常接待（客户可能正在等回复）。
    since = (account.since - _HISTORY_GRACE).timestamp()
    fresh = [m for m in msgs if m.get("msgid") and int(m.get("send_time") or 0) >= since]
    async with ctx.db.tenant_session(tenant.id) as session:
        known = set(
            (
                await session.scalars(
                    select(Message.ext_msg_id).where(
                        Message.channel_account_id == account.channel_account_id,
                        Message.ext_msg_id.in_([m["msgid"] for m in fresh]),
                    )
                )
            ).all()
        )
        new = [m for m in fresh if m["msgid"] not in known]
        users = {_customer_of(m) for m in new} - {None}
        known_users = set(
            (
                await session.scalars(
                    select(CustomerIdentity.external_id).where(
                        CustomerIdentity.channel_account_id == account.channel_account_id,
                        CustomerIdentity.external_id.in_(users),
                    )
                )
            ).all()
        )
        servicers = {str(m["servicer_userid"]) for m in new if m.get("servicer_userid")}
        staff_by_userid = dict(
            (
                await session.execute(
                    select(Staff.wecom_userid, Staff.id).where(Staff.wecom_userid.in_(servicers))
                )
            ).all()
        )
        channel = await session.get(ChannelAccount, account.channel_account_id)
        kf_config = ((channel.config or {}).get("kf") or {}) if channel is not None else {}
    # 外部调用（客户资料、媒体下载）在数据库事务之外进行。
    profiles = await _profiles(wecom, account.corp_id, {u for u in users if u} - known_users)
    contents: dict[str, tuple[str, dict[str, Any], str | None]] = {}
    for m in new:
        if int(m.get("origin") or 0) in (ORIGIN_CUSTOMER, ORIGIN_SERVICER):
            contents[m["msgid"]] = await _content(ctx, wecom, tenant.code, account.corp_id, m)

    batch = _Batch()
    now = utcnow()
    async with ctx.db.tenant_session(tenant.id) as session:
        for m in new:
            origin = int(m.get("origin") or 0)
            if origin in (ORIGIN_CUSTOMER, ORIGIN_SERVICER) and m.get("external_userid"):
                identity, room = await _identity_room(
                    session, tenant, account, str(m["external_userid"]), profiles
                )
                sender_staff = staff_by_userid.get(m.get("servicer_userid"))
                message_id = await _insert(
                    session, room, identity, m, contents[m["msgid"]], origin, sender_staff
                )
                if message_id is None:
                    continue
                outbox.enqueue_mirror(session, room.id, message_id)
                batch.new_messages.append((room.id, message_id))
                batch.rooms.add(room.id)
                if origin == ORIGIN_CUSTOMER:
                    batch.customers.add(identity.external_id)
            elif origin == ORIGIN_EVENT and m.get("msgtype") == "event":
                await _on_event(
                    session, tenant, account, m.get("event") or {}, kf_config, profiles, batch
                )
        row = await session.get(WecomKfAccount, account.id)
        if row is not None:
            if next_cursor:
                row.cursor = next_cursor
            row.synced_at = now
        await session.commit()

    for room_id, message_id in batch.new_messages:
        try:
            await ctx.bus.publish(message_received(tenant.id, room_id, message_id))
        except Exception:
            # 消息已经入库；没有归入会话的消息会被调度进程重新发布。
            logger.exception("publishing message.received for %s failed", message_id)
    await outbox.flush_rooms(ctx, tenant.id, batch.rooms)
    for external_userid in batch.reset_states:
        await ctx.redis.delete(_state_key(account, external_userid))
    for external_userid in batch.customers:
        await _ensure_bot_state(ctx, wecom, account, external_userid)
    for room_id, code, text in batch.welcomes:
        await _send_welcome(ctx, wecom, tenant.id, account, room_id, code, text)
    return len(batch.new_messages)


def _customer_of(m: dict[str, Any]) -> str | None:
    if m.get("external_userid"):
        return str(m["external_userid"])
    event = m.get("event") or {}
    return str(event["external_userid"]) if event.get("external_userid") else None


async def _profiles(
    wecom: WeComClient, corp_id: str, external_userids: set[str]
) -> dict[str, dict[str, Any]]:
    """新客户的昵称、头像、unionid（每次最多 100 位）。取不到时用默认名称。"""
    result: dict[str, dict[str, Any]] = {}
    ids = sorted(external_userids)
    for start in range(0, len(ids), 100):
        try:
            data = await wecom.corp_call(
                corp_id,
                "POST",
                "/cgi-bin/kf/customer/batchget",
                json={"external_userid_list": ids[start : start + 100]},
            )
        except WeComError as exc:
            logger.warning("kf customer batchget failed: %s", exc)
            continue
        for customer in data.get("customer_list") or []:
            if customer.get("external_userid"):
                result[str(customer["external_userid"])] = customer
    return result


def _profile(data: dict[str, Any]) -> dict[str, Any]:
    return {
        key: data[key]
        for key in ("nickname", "avatar", "gender", "unionid")
        if data.get(key) not in (None, "")
    }


async def _identity_room(
    session: AsyncSession,
    tenant: Tenant,
    account: _Account,
    external_userid: str,
    profiles: dict[str, dict[str, Any]],
) -> tuple[CustomerIdentity, Room]:
    identity = await session.scalar(
        select(CustomerIdentity).where(
            CustomerIdentity.channel_account_id == account.channel_account_id,
            CustomerIdentity.external_id == external_userid,
        )
    )
    if identity is None:
        profile = _profile(profiles.get(external_userid) or {})
        customer = await customer_for(session, tenant, external_userid, profile)
        identity_id = new_id()
        identity = CustomerIdentity(
            id=identity_id,
            tenant_id=tenant.id,
            customer_id=customer.id,
            channel_account_id=account.channel_account_id,
            external_id=external_userid,
            im_user_id=imids.customer_user(tenant.code, identity_id),
            profile=profile,
        )
        session.add(identity)
        await session.flush()
    room = await session.scalar(select(Room).where(Room.identity_id == identity.id))
    if room is None:
        room_id = new_id()
        room = Room(
            id=room_id,
            tenant_id=tenant.id,
            customer_id=identity.customer_id,
            identity_id=identity.id,
            channel_account_id=account.channel_account_id,
            im_group_id=imids.room_group(tenant.code, room_id),
        )
        session.add(room)
        await session.flush()
    return identity, room


async def customer_for(
    session: AsyncSession,
    tenant: Tenant,
    external_userid: str,
    profile: dict[str, Any],
    *,
    source: str = ChannelType.WECOM_KF,
) -> Customer:
    """同一企业的外部联系人或其他客服账号里已有这位客户时，归到同一份档案；否则新建。"""
    other = await session.scalar(
        select(CustomerIdentity)
        .join(ChannelAccount, ChannelAccount.id == CustomerIdentity.channel_account_id)
        .where(
            CustomerIdentity.external_id == external_userid,
            ChannelAccount.type.in_([ChannelType.WECOM_KF, ChannelType.WECOM_CONTACT]),
        )
        .limit(1)
    )
    unionid = profile.get("unionid")
    if other is None and unionid:
        other = await session.scalar(
            select(CustomerIdentity)
            .where(CustomerIdentity.profile["unionid"].astext == str(unionid))
            .limit(1)
        )
    if other is not None:
        found = await session.get(Customer, other.customer_id)
        if found is not None:
            return found
    customer = Customer(
        id=new_id(),
        tenant_id=tenant.id,
        display_name=str(profile.get("nickname") or f"微信用户 {external_userid[-4:]}")[:128],
        source_channel=source,
    )
    session.add(customer)
    await session.flush()
    return customer


async def _insert(
    session: AsyncSession,
    room: Room,
    identity: CustomerIdentity,
    m: dict[str, Any],
    content: tuple[str, dict[str, Any], str | None],
    origin: int,
    staff_id: UUID | None,
) -> UUID | None:
    content_type, body, text = content
    sent_at = datetime.fromtimestamp(int(m.get("send_time") or 0), UTC)
    from_customer = origin == ORIGIN_CUSTOMER
    if not from_customer:
        body = {**body, "servicer_userid": m.get("servicer_userid")}
    stmt = (
        insert(Message)
        .values(
            id=new_id(),
            tenant_id=room.tenant_id,
            room_id=room.id,
            channel_account_id=room.channel_account_id,
            direction=Direction.IN if from_customer else Direction.OUT,
            sender_type=SenderType.CUSTOMER if from_customer else SenderType.AGENT,
            sender_id=identity.id if from_customer else staff_id,
            content_type=content_type,
            content=body,
            text_plain=text,
            ext_msg_id=str(m["msgid"]),
            source=MessageSource.CHANNEL,
            send_status=None if from_customer else SendStatus.SENT,
            sent_at=sent_at,
        )
        .on_conflict_do_nothing(constraint="uq_messages_ext_msg")
        .returning(Message.id)
    )
    message_id = (await session.execute(stmt)).scalar_one_or_none()
    if message_id is None:
        return None
    room.last_message_at = max(room.last_message_at or sent_at, sent_at)
    room.last_active_at = utcnow()
    if from_customer:
        identity.last_seen_at = sent_at
    return message_id


async def _content(
    ctx: AppContext, wecom: WeComClient, tenant_code: str, corp_id: str, m: dict[str, Any]
) -> tuple[str, dict[str, Any], str | None]:
    """把微信客服的消息转成平台的消息类型、内容和纯文本。"""
    msgtype = str(m.get("msgtype") or "")
    body = m.get(msgtype) if isinstance(m.get(msgtype), dict) else {}
    assert isinstance(body, dict)
    if msgtype == "text":
        text = str(body.get("content") or "")
        content: dict[str, Any] = {"text": text}
        if body.get("menu_id"):
            content["menu_id"] = body["menu_id"]
        return "text", content, text
    if msgtype in _MEDIA_LABELS:
        label = f"[{_MEDIA_LABELS[msgtype]}]"
        try:
            stored = await _store_media(ctx, wecom, tenant_code, corp_id, body, msgtype, m)
        except (WeComError, StorageError) as exc:
            logger.warning("kf media %s not stored: %s", m.get("msgid"), exc)
            return "text", {"text": label, "media_error": str(exc)[:200]}, label
        return msgtype, stored, None
    text = _describe(msgtype, body)
    return "text", {"text": text, msgtype or "raw": body}, text


def _describe(msgtype: str, body: dict[str, Any]) -> str:
    match msgtype:
        case "location":
            place = " ".join(str(body.get(k) or "") for k in ("name", "address")).strip()
            return f"[位置] {place}".strip()
        case "link":
            return f"[链接] {body.get('title') or ''} {body.get('url') or ''}".strip()
        case "miniprogram":
            return f"[小程序] {body.get('title') or ''}".strip()
        case "business_card":
            return "[名片]"
        case "msgmenu":
            return str(body.get("head_content") or "[菜单消息]")
        case _:
            return f"[{msgtype or '未知消息'}]"


async def _store_media(
    ctx: AppContext,
    wecom: WeComClient,
    tenant_code: str,
    corp_id: str,
    body: dict[str, Any],
    msgtype: str,
    m: dict[str, Any],
) -> dict[str, Any]:
    media_id = body.get("media_id")
    if not media_id:
        raise WeComError(0, "media_id missing", "media/get")
    media = await wecom.download_media(corp_id, str(media_id))
    extension = mimetypes.guess_extension(media.content_type) or ""
    if msgtype == "voice" and not extension:
        extension = ".amr"
    name = safe_filename(media.filename or f"{msgtype}-{str(m['msgid'])[-8:]}{extension}")
    key = f"{tenant_code}/wecom/{utcnow():%Y/%m}/{uuid.uuid4().hex}/{name}"
    await ctx.storage.put(key, media.data, media.content_type)
    return {
        "url": file_url(ctx.settings, key),
        "name": name,
        "size": len(media.data),
        "width": None,
        "height": None,
        "mime": media.content_type,
    }


async def _on_event(
    session: AsyncSession,
    tenant: Tenant,
    account: _Account,
    event: dict[str, Any],
    kf_config: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    batch: _Batch,
) -> None:
    event_type = event.get("event_type")
    external_userid = str(event.get("external_userid") or "")
    match event_type:
        case "enter_session" if external_userid:
            identity, room = await _identity_room(
                session, tenant, account, external_userid, profiles
            )
            scene = {k: event[k] for k in ("scene", "scene_param") if event.get(k)}
            if scene:
                identity.profile = {**(identity.profile or {}), "entry": scene}
            welcome = str(kf_config.get("welcome_message") or "").strip()
            code = event.get("welcome_code")
            if welcome and code:
                batch.welcomes.append((room.id, str(code), welcome))
        case "msg_send_fail":
            reason = _FAIL_TYPES.get(int(event.get("fail_type") or 0), "未知原因")
            await session.execute(
                update(Message)
                .where(
                    Message.channel_account_id == account.channel_account_id,
                    Message.ext_msg_id == str(event.get("fail_msgid") or ""),
                )
                .values(send_status=SendStatus.FAILED, send_error=f"微信客服投递失败：{reason}")
            )
        case "user_recall_msg" | "servicer_recall_msg":
            recalled = await session.scalar(
                select(Message).where(
                    Message.channel_account_id == account.channel_account_id,
                    Message.ext_msg_id == str(event.get("recall_msgid") or ""),
                )
            )
            if recalled is not None:
                recalled.content = {**recalled.content, "recalled": True}
        case "session_status_change" if external_userid:
            batch.reset_states.add(external_userid)
        case _:
            pass


def _state_key(account: _Account, external_userid: str) -> str:
    return f"edp:wecom:kfstate:{account.corp_id}:{account.open_kfid}:{external_userid}"


async def _ensure_bot_state(
    ctx: AppContext, wecom: WeComClient, account: _Account, external_userid: str
) -> None:
    """新接入（0）的会话置为由智能助手接待（1）。结果缓存 10 分钟，避免每条消息都查询。"""
    key = _state_key(account, external_userid)
    if await ctx.redis.get(key):
        return
    target = {"open_kfid": account.open_kfid, "external_userid": external_userid}
    try:
        data = await wecom.corp_call(
            account.corp_id, "POST", "/cgi-bin/kf/service_state/get", json=target
        )
        state = int(data.get("service_state") or 0)
        if state == STATE_NEW:
            await wecom.corp_call(
                account.corp_id,
                "POST",
                "/cgi-bin/kf/service_state/trans",
                json={**target, "service_state": STATE_BOT},
            )
            state = STATE_BOT
        await ctx.redis.set(key, str(state), ex=_STATE_TTL)
    except WeComError as exc:
        logger.warning("kf service state for %s failed: %s", external_userid, exc)


async def _send_welcome(
    ctx: AppContext,
    wecom: WeComClient,
    tenant_id: UUID,
    account: _Account,
    room_id: UUID,
    code: str,
    text: str,
) -> None:
    """用 enter_session 事件的 welcome_code 发送欢迎语（不占 5 条额度），并记入会话。"""
    message_id = new_id()
    try:
        data = await wecom.corp_call(
            account.corp_id,
            "POST",
            "/cgi-bin/kf/send_msg_on_event",
            json={
                "code": code,
                "msgid": message_id.hex,
                "msgtype": "text",
                "text": {"content": _clip(text)},
            },
        )
    except WeComError as exc:
        logger.warning("kf welcome message failed: %s", exc)
        return
    async with ctx.db.tenant_session(tenant_id) as session:
        session.add(
            Message(
                id=message_id,
                tenant_id=tenant_id,
                room_id=room_id,
                channel_account_id=account.channel_account_id,
                direction=Direction.OUT,
                sender_type=SenderType.SYSTEM,
                content_type="text",
                content={"text": text, "welcome": True},
                text_plain=text,
                ext_msg_id=str(data.get("msgid") or message_id.hex),
                source=MessageSource.API,
                send_status=SendStatus.SENT,
                sent_at=utcnow(),
            )
        )
        await session.flush()
        outbox.enqueue_mirror(session, room_id, message_id)
        await session.commit()
    await ctx.bus.publish(message_received(tenant_id, room_id, message_id))
    await outbox.flush_rooms(ctx, tenant_id, [room_id])


async def sync_all(ctx: AppContext) -> int:
    """兜底拉取（调度进程）：回调丢失时也不漏消息。"""
    if ctx.wecom is None:
        return 0
    async with ctx.db.platform_sessionmaker() as session:
        rows = (
            await session.execute(
                select(WecomKfAccount.tenant_id, WecomKfAccount.open_kfid)
                .join(
                    WecomCorp,
                    (WecomCorp.tenant_id == WecomKfAccount.tenant_id)
                    & (WecomCorp.status == CorpStatus.ACTIVE),
                )
                .where(WecomKfAccount.status == KfAccountStatus.ACTIVE)
            )
        ).all()
    total = 0
    for tenant_id, open_kfid in rows:
        try:
            total += await sync_messages(ctx, tenant_id, open_kfid)
        except WeComError as exc:
            logger.warning("kf fallback sync for %s failed: %s", open_kfid, exc)
    return total


# ---- 出站 ----


@dataclass(frozen=True)
class ReplyWindow:
    open: bool
    deadline: datetime | None
    remaining: int
    reason: str | None


async def reply_window(session: AsyncSession, room_id: UUID, now: datetime) -> ReplyWindow:
    last_in = await session.scalar(
        select(func.max(Message.sent_at)).where(
            Message.room_id == room_id, Message.sender_type == SenderType.CUSTOMER
        )
    )
    if last_in is None:
        return ReplyWindow(False, None, 0, NO_CUSTOMER_MESSAGE)
    deadline = last_in + KF_WINDOW
    if now >= deadline:
        return ReplyWindow(False, deadline, 0, WINDOW_EXPIRED)
    used = await session.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.room_id == room_id,
            Message.direction == Direction.OUT,
            Message.source == MessageSource.API,
            Message.ext_msg_id.is_not(None),
            Message.sent_at >= last_in,
            ~Message.content.has_key("welcome"),
        )
    )
    remaining = max(0, KF_WINDOW_LIMIT - int(used or 0))
    if remaining == 0:
        return ReplyWindow(False, deadline, 0, WINDOW_USED_UP)
    return ReplyWindow(True, deadline, remaining, None)


async def session_reply_window(session: AsyncSession, chat: ChatSession) -> ReplyWindowOut:
    channel = await session.get(ChannelAccount, chat.channel_account_id)
    if channel is None or channel.type != ChannelType.WECOM_KF:
        return ReplyWindowOut(
            limited=False, open=True, deadline=None, remaining=None, limit=None, reason=None
        )
    window = await reply_window(session, chat.room_id, utcnow())
    return ReplyWindowOut(
        limited=True,
        open=window.open,
        deadline=window.deadline,
        remaining=window.remaining,
        limit=KF_WINDOW_LIMIT,
        reason=window.reason,
    )


def _clip(text: str, limit: int = TEXT_LIMIT_BYTES) -> str:
    """文本消息最长 2048 字节（UTF-8）。"""
    data = text.encode()
    if len(data) <= limit:
        return text
    return data[: limit - 3].decode(errors="ignore") + "…"


def _fail(message: Message, reason: str) -> None:
    message.send_status = SendStatus.FAILED
    message.send_error = reason[:500]


async def deliver(
    ctx: AppContext, session: AsyncSession, room: Room, message: Message, now: datetime
) -> bool:
    """把一条出站消息投递给微信客户（发件箱调用，已持有 Room 的锁）。

    成功返回 True；回复窗口已关闭、账号停用或企业微信拒绝时把消息标记为失败并返回 False；
    网络故障抛出 WeComUnavailable，由发件箱稍后重试。
    """
    account = await session.scalar(
        select(WecomKfAccount).where(WecomKfAccount.channel_account_id == room.channel_account_id)
    )
    identity = await session.get(CustomerIdentity, room.identity_id)
    if (
        ctx.wecom is None
        or account is None
        or identity is None
        or account.status != KfAccountStatus.ACTIVE
    ):
        _fail(message, ACCOUNT_UNAVAILABLE)
        return False
    window = await reply_window(session, room.id, now)
    if not window.open:
        _fail(message, window.reason or WINDOW_EXPIRED)
        return False
    try:
        body = await _outbound(ctx, ctx.wecom, account.corp_id, message)
        data = await ctx.wecom.corp_call(
            account.corp_id,
            "POST",
            "/cgi-bin/kf/send_msg",
            json={
                "touser": identity.external_id,
                "open_kfid": account.open_kfid,
                "msgid": message.id.hex,
                **body,
            },
        )
    except WeComUnavailable:
        raise
    except WeComError as exc:
        _fail(message, f"企业微信拒绝发送：{exc.errmsg or exc.errcode}（{exc.errcode}）")
        return False
    message.ext_msg_id = str(data.get("msgid") or message.id.hex)
    message.send_status = SendStatus.SENT
    message.send_error = None
    message.sent_at = now
    return True


async def _outbound(
    ctx: AppContext, wecom: WeComClient, corp_id: str, message: Message
) -> dict[str, Any]:
    content = message.content or {}
    if message.content_type in ("image", "file") and content.get("url"):
        key = key_of_url(ctx.settings, str(content["url"]))
        if key is None:
            raise WeComError(0, "附件不是平台签发的文件链接", "media/upload")
        data = await ctx.storage.get(key)
        kind = str(message.content_type)
        media_id = await wecom.upload_media(
            corp_id,
            kind,
            str(content.get("name") or key.rsplit("/", 1)[-1]),
            data,
            str(content.get("mime") or "application/octet-stream"),
        )
        return {"msgtype": kind, kind: {"media_id": media_id}}
    text = message.text_plain or str(content.get("text") or "")
    if message.sender_type == SenderType.BOT:
        text = f"{AI_LABEL}{text}"
    return {"msgtype": "text", "text": {"content": _clip(text)}}
