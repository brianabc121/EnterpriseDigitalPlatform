"""企业微信通讯录、客户联系与客户群（设计文档 §10.4）。

- 成员：user/list_id 拉取成员 ID（代开发应用拿不到手机号、邮箱），姓名用 user/get 补齐；
  与平台员工按 staff.wecom_userid 绑定（管理员在控制台绑定，或员工扫码登录时自动绑定）。
- 客户：按配置了客户联系功能的成员全量拉取外部联系人（batch/get_by_user），之后按
  change_external_contact 回调增量更新。写入客户档案和身份（客户联系渠道）；同一企业的微信客服
  客户归到同一份档案。客户还没有归属坐席时，最早添加他的已绑定员工成为归属坐席。
- 标签：企业标签按名称对应客户标签。企业微信里的标签以企业微信为准（各添加人打的标签取并集），
  平台上打的标签写回企业微信（mark_tag）；平台自有的标签不受影响。
- 新客户欢迎语：add_external_contact 回调带 WelcomeCode 时发送，可附带微信客服链接（AI 客服入口）。
- 客户群：同步群列表、群成员，外部成员关联客户档案；change_external_chat 回调增量更新。
- 客户继承：平台上转移客户时可以同步变更企业微信里的添加人，结果定时回收（设计 §14.3）。
  原添加人在职时走在职继承（transfer_customer：90 天内每位客户最多转接 2 次，24 小时后自动接替），
  已离职时走离职继承（resigned/transfer_customer）。
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.ids import new_id
from app.integrations.wecom import WeComClient, WeComError
from app.modules.channels.models import ChannelAccount, ChannelType
from app.modules.conversation import imids
from app.modules.customer.models import (
    Customer,
    CustomerIdentity,
    CustomerOwnerHistory,
    OwnerChangeReason,
)
from app.modules.customer.ownership import change_owner
from app.modules.customer.sensitive import normalize_phone, set_phone, valid_phone
from app.modules.iam.models import Staff, StaffStatus
from app.modules.security.keys import TenantKeyring
from app.modules.tenancy.models import Tenant
from app.modules.wecom import kf
from app.modules.wecom.models import (
    GroupChatStatus,
    GroupMemberType,
    MemberStatus,
    TransferKind,
    TransferStatus,
    WecomContactFollow,
    WecomCorp,
    WecomGroupChat,
    WecomGroupMember,
    WecomKfAccount,
    WecomMember,
    WecomTag,
    WecomTransfer,
)
from app.modules.wecom.schemas import WecomSettings
from app.modules.wecom.service import (
    active_corp,
    client_of,
    ensure_contact_channel,
    record_sync,
)

logger = logging.getLogger(__name__)

_USER_NAME_BATCH = 500
_TRANSFER_BATCH = 100
# (提交转接, 查询结果) 的接口。
_TRANSFER_PATHS = {
    TransferKind.ONJOB: (
        "/cgi-bin/externalcontact/transfer_customer",
        "/cgi-bin/externalcontact/transfer_result",
    ),
    TransferKind.RESIGNED: (
        "/cgi-bin/externalcontact/resigned/transfer_customer",
        "/cgi-bin/externalcontact/resigned/transfer_result",
    ),
}
_TRANSFER_RESULTS = {
    1: (TransferStatus.SUCCESS, None),
    2: (TransferStatus.WAITING, None),
    3: (TransferStatus.FAILED, "客户拒绝了转接"),
    4: (TransferStatus.FAILED, "接替成员的客户数已达上限"),
    5: (TransferStatus.FAILED, "企业微信里没有这条转接记录"),
}
WELCOME_LINK_TITLE = "在线客服"
WELCOME_LINK_DESC = "有问题随时咨询，AI 客服 7×24 小时为您解答"


def utcnow() -> datetime:
    return datetime.now(UTC)


def _ts(value: Any) -> datetime | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, UTC) if seconds > 0 else None


@dataclass(frozen=True)
class _Corp:
    tenant: Tenant
    corp: WecomCorp


async def _load(ctx: AppContext, tenant_id: UUID) -> _Corp | None:
    async with ctx.db.tenant_session(tenant_id) as session:
        corp = await active_corp(session)
        tenant = await session.get(Tenant, tenant_id)
    if corp is None or tenant is None:
        return None
    return _Corp(tenant, corp)


# ---- 成员 ----


async def sync_members(ctx: AppContext, tenant_id: UUID) -> int:
    wecom = client_of(ctx)
    loaded = await _load(ctx, tenant_id)
    if loaded is None:
        return 0
    corp_id = loaded.corp.corp_id
    departments: dict[str, list[int]] = defaultdict(list)
    cursor = ""
    while True:
        data = await wecom.corp_call(
            corp_id, "POST", "/cgi-bin/user/list_id", json={"cursor": cursor, "limit": 10000}
        )
        for entry in data.get("dept_user") or []:
            userid = str(entry.get("userid") or "")
            if userid:
                department = entry.get("department")
                if isinstance(department, int):
                    departments[userid].append(department)
                else:
                    departments.setdefault(userid, [])
        cursor = str(data.get("next_cursor") or "")
        if not cursor:
            break
    follow = await _follow_users(wecom, corp_id)
    now = utcnow()
    async with ctx.db.tenant_session(tenant_id) as session:
        existing = {m.userid: m for m in (await session.scalars(select(WecomMember))).all()}
        for userid, depts in departments.items():
            member = existing.get(userid)
            if member is None:
                member = WecomMember(tenant_id=tenant_id, userid=userid)
                session.add(member)
            member.departments = sorted(set(depts))
            member.follow = userid in follow
            member.status = MemberStatus.ACTIVE
            member.synced_at = now
        for userid, member in existing.items():
            if userid not in departments:
                member.status = MemberStatus.LEFT
        await session.commit()
        unnamed = (
            await session.scalars(
                select(WecomMember.userid)
                .where(WecomMember.status == MemberStatus.ACTIVE, WecomMember.name == "")
                .limit(_USER_NAME_BATCH)
            )
        ).all()
    names: dict[str, str] = {}
    for userid in unnamed:
        try:
            data = await wecom.corp_call(
                corp_id, "GET", "/cgi-bin/user/get", params={"userid": userid}
            )
        except WeComError as exc:
            logger.info("user/get %s failed: %s", userid, exc)
            continue
        if data.get("name"):
            names[userid] = str(data["name"])[:128]
    if names:
        async with ctx.db.tenant_session(tenant_id) as session:
            for userid, name in names.items():
                await session.execute(
                    update(WecomMember).where(WecomMember.userid == userid).values(name=name)
                )
            await session.commit()
    await record_sync(ctx, tenant_id, "members", count=len(departments))
    return len(departments)


async def _follow_users(wecom: WeComClient, corp_id: str) -> set[str]:
    """配置了客户联系功能的成员。"""
    try:
        data = await wecom.corp_call(
            corp_id, "GET", "/cgi-bin/externalcontact/get_follow_user_list"
        )
    except WeComError as exc:
        logger.warning("get_follow_user_list failed: %s", exc)
        return set()
    return {str(u) for u in data.get("follow_user") or []}


async def on_member_change(ctx: AppContext, tenant_id: UUID, event: dict[str, Any]) -> None:
    """通讯录变更（change_contact）：成员新增、删除、修改。"""
    change = event.get("ChangeType")
    userid = str(event.get("UserID") or "")
    if not userid:
        return
    async with ctx.db.tenant_session(tenant_id) as session:
        member = await session.scalar(select(WecomMember).where(WecomMember.userid == userid))
        if change == "delete_user":
            if member is not None:
                member.status = MemberStatus.LEFT
        elif change in ("create_user", "update_user"):
            if member is None:
                member = WecomMember(tenant_id=tenant_id, userid=userid)
                session.add(member)
            new_id_value = event.get("NewUserID")
            if change == "update_user" and new_id_value:
                member.userid = str(new_id_value)
                await session.execute(
                    update(Staff)
                    .where(Staff.wecom_userid == userid)
                    .values(wecom_userid=member.userid)
                )
            if event.get("Name"):
                member.name = str(event["Name"])[:128]
            member.status = MemberStatus.ACTIVE
            member.synced_at = utcnow()
        await session.commit()


# ---- 标签 ----


async def sync_tags(ctx: AppContext, tenant_id: UUID) -> int:
    wecom = client_of(ctx)
    loaded = await _load(ctx, tenant_id)
    if loaded is None:
        return 0
    data = await wecom.corp_call(
        loaded.corp.corp_id, "POST", "/cgi-bin/externalcontact/get_corp_tag_list", json={}
    )
    now = utcnow()
    seen: set[str] = set()
    async with ctx.db.tenant_session(tenant_id) as session:
        for group in data.get("tag_group") or []:
            for tag in group.get("tag") or []:
                tag_id = str(tag.get("id") or "")
                if not tag_id:
                    continue
                seen.add(tag_id)
                values = {
                    "name": str(tag.get("name") or "")[:64],
                    "group_id": group.get("group_id"),
                    "group_name": (str(group.get("group_name") or "")[:64] or None),
                    "sort": int(tag.get("order") or 0),
                    "deleted": bool(tag.get("deleted")),
                    "synced_at": now,
                }
                await session.execute(
                    insert(WecomTag)
                    .values(tenant_id=tenant_id, tag_id=tag_id, **values)
                    .on_conflict_do_update(index_elements=["tenant_id", "tag_id"], set_=values)
                )
        await session.execute(
            update(WecomTag).where(WecomTag.tag_id.not_in(seen)).values(deleted=True)
        )
        await session.commit()
    await record_sync(ctx, tenant_id, "tags", count=len(seen))
    return len(seen)


async def _tag_names(session: AsyncSession) -> dict[str, str]:
    rows = await session.execute(
        select(WecomTag.tag_id, WecomTag.name).where(WecomTag.deleted.is_(False))
    )
    return {tag_id: name for tag_id, name in rows}


# ---- 客户 ----


async def sync_contacts(ctx: AppContext, tenant_id: UUID) -> int:
    """全量同步：按配置了客户联系功能的成员拉取外部联系人（每批 100 位成员）。"""
    wecom = client_of(ctx)
    loaded = await _load(ctx, tenant_id)
    if loaded is None:
        return 0
    corp_id = loaded.corp.corp_id
    follow = sorted(await _follow_users(wecom, corp_id))
    seen: set[tuple[str, str]] = set()
    for start in range(0, len(follow), 100):
        cursor = ""
        while True:
            data = await wecom.corp_call(
                corp_id,
                "POST",
                "/cgi-bin/externalcontact/batch/get_by_user",
                json={"userid_list": follow[start : start + 100], "cursor": cursor, "limit": 100},
            )
            entries = data.get("external_contact_list") or []
            async with ctx.db.tenant_session(tenant_id) as session:
                for entry in entries:
                    contact = entry.get("external_contact") or {}
                    info = entry.get("follow_info") or {}
                    pair = await _upsert(session, ctx.keys, loaded.tenant, contact, [info])
                    seen |= pair
                await session.commit()
            cursor = str(data.get("next_cursor") or "")
            if not cursor:
                break
    async with ctx.db.tenant_session(tenant_id) as session:
        follows = (
            await session.scalars(
                select(WecomContactFollow).where(WecomContactFollow.deleted_at.is_(None))
            )
        ).all()
        now = utcnow()
        for row in follows:
            if (row.external_userid, row.userid) not in seen and row.userid in follow:
                row.deleted_at = now
        await session.commit()
    contacts = len({external for external, _ in seen})
    await record_sync(ctx, tenant_id, "contacts", count=contacts)
    return contacts


async def refresh_contact(ctx: AppContext, tenant_id: UUID, external_userid: str) -> None:
    """单个客户的详情和全部添加人（回调里增量更新时使用）。"""
    wecom = client_of(ctx)
    loaded = await _load(ctx, tenant_id)
    if loaded is None:
        return
    data = await wecom.corp_call(
        loaded.corp.corp_id,
        "GET",
        "/cgi-bin/externalcontact/get",
        params={"external_userid": external_userid},
    )
    async with ctx.db.tenant_session(tenant_id) as session:
        seen = await _upsert(
            session,
            ctx.keys,
            loaded.tenant,
            data.get("external_contact") or {},
            data.get("follow_user") or [],
        )
        # 这次返回里没有的添加人：关系已经解除。
        await session.execute(
            update(WecomContactFollow)
            .where(
                WecomContactFollow.external_userid == external_userid,
                WecomContactFollow.deleted_at.is_(None),
                WecomContactFollow.userid.not_in({userid for _, userid in seen}),
            )
            .values(deleted_at=utcnow())
        )
        await session.commit()


def _contact_profile(contact: dict[str, Any]) -> dict[str, Any]:
    profile = {
        "nickname": contact.get("name"),
        "avatar": contact.get("avatar"),
        "gender": contact.get("gender"),
        "unionid": contact.get("unionid"),
        "corp_name": contact.get("corp_name"),
        "type": contact.get("type"),
    }
    return {k: v for k, v in profile.items() if v not in (None, "")}


async def _upsert(
    session: AsyncSession,
    keys: TenantKeyring,
    tenant: Tenant,
    contact: dict[str, Any],
    follows: list[dict[str, Any]],
) -> set[tuple[str, str]]:
    """写入一位外部联系人及其添加人，返回 (external_userid, userid) 集合。"""
    external_userid = str(contact.get("external_userid") or "")
    if not external_userid:
        return set()
    channel = await ensure_contact_channel(session, tenant.id, tenant.code)
    profile = _contact_profile(contact)
    identity = await session.scalar(
        select(CustomerIdentity).where(
            CustomerIdentity.channel_account_id == channel.id,
            CustomerIdentity.external_id == external_userid,
        )
    )
    remark = next((str(i.get("remark")) for i in follows if i.get("remark")), None)
    if identity is None:
        # 新客户的名称优先用员工给他的备注名。
        named = {**profile, "nickname": remark or profile.get("nickname")}
        customer = await kf.customer_for(
            session, tenant, external_userid, named, source=ChannelType.WECOM_CONTACT
        )
        identity_id = new_id()
        identity = CustomerIdentity(
            id=identity_id,
            tenant_id=tenant.id,
            customer_id=customer.id,
            channel_account_id=channel.id,
            external_id=external_userid,
            im_user_id=imids.customer_user(tenant.code, identity_id),
            profile=profile,
        )
        session.add(identity)
        await session.flush()
    else:
        identity.profile = {**(identity.profile or {}), **profile}
        found = await session.get(Customer, identity.customer_id)
        assert found is not None
        customer = found
    tag_names = await _tag_names(session)
    seen: set[tuple[str, str]] = set()
    added: list[tuple[datetime, str]] = []
    for info in follows:
        userid = str(info.get("userid") or "")
        if not userid:
            continue
        seen.add((external_userid, userid))
        tag_ids = [str(t) for t in info.get("tag_id") or []] or [
            str(t.get("tag_id")) for t in info.get("tags") or [] if t.get("tag_id")
        ]
        added_at = _ts(info.get("createtime"))
        values = {
            "customer_id": customer.id,
            "remark": (str(info.get("remark") or "")[:128] or None),
            "description": info.get("description") or None,
            "tag_ids": tag_ids,
            "add_way": info.get("add_way"),
            "state": (str(info.get("state") or "")[:64] or None),
            "added_at": added_at,
            "deleted_at": None,
            "updated_at": utcnow(),
        }
        await session.execute(
            insert(WecomContactFollow)
            .values(
                id=new_id(),
                tenant_id=tenant.id,
                external_userid=external_userid,
                userid=userid,
                **values,
            )
            .on_conflict_do_update(constraint="uq_wecom_contact_follows", set_=values)
        )
        added.append((added_at or utcnow(), userid))
    # 客户标签：企业标签以企业微信为准（各添加人的并集），平台自有的标签保留。
    active_tags = (
        await session.scalars(
            select(WecomContactFollow.tag_ids).where(
                WecomContactFollow.external_userid == external_userid,
                WecomContactFollow.deleted_at.is_(None),
            )
        )
    ).all()
    corp_names = set(tag_names.values())
    wecom_tags = {tag_names[t] for ids in active_tags for t in ids if t in tag_names}
    merged = [t for t in customer.tags or [] if t not in corp_names] + sorted(wecom_tags)
    if merged != list(customer.tags or []):
        customer.tags = merged
    # 自动生成的名称（"微信用户 xxxx"或微信昵称）换成备注名；员工在平台上改过的名称不动。
    name = remark or profile.get("nickname")
    automatic = customer.display_name.startswith("微信用户 ") or customer.display_name == (
        profile.get("nickname") or ""
    )
    if name and automatic and customer.display_name != name:
        customer.display_name = str(name)[:128]
    await _fill_profile(keys, customer, contact, follows)
    if customer.owner_id is None and added:
        # 最早添加这位客户、且绑定了平台员工的成员。
        bound = dict(
            (
                await session.execute(
                    select(Staff.wecom_userid, Staff.id).where(
                        Staff.wecom_userid.in_([userid for _, userid in added]),
                        Staff.status == StaffStatus.ACTIVE,
                    )
                )
            ).all()
        )
        first = next((userid for _, userid in sorted(added) if userid in bound), None)
        if first is not None:
            await change_owner(
                session,
                customer,
                bound[first],
                actor_id=None,
                reason=OwnerChangeReason.WECOM,
                note=f"企业微信成员 {first} 添加了这位客户",
            )
    return seen


async def _fill_profile(
    keys: TenantKeyring,
    customer: Customer,
    contact: dict[str, Any],
    follows: list[dict[str, Any]],
) -> None:
    """资料补全：员工在企业微信里备注的手机号和企业名称（平台上已经填写的不覆盖）。"""
    if not customer.phone_enc:
        mobiles = (normalize_phone(str(m)) for i in follows for m in i.get("remark_mobiles") or [])
        phone = next((m for m in mobiles if valid_phone(m)), None)
        if phone:
            await set_phone(keys, customer, phone)
    if not customer.company:
        company = next(
            (str(i["remark_corp_name"]) for i in follows if i.get("remark_corp_name")),
            str(contact.get("corp_name") or ""),
        )
        if company:
            customer.company = company[:128]


async def on_contact_change(ctx: AppContext, tenant_id: UUID, event: dict[str, Any]) -> None:
    change = event.get("ChangeType")
    userid = str(event.get("UserID") or "")
    external_userid = str(event.get("ExternalUserID") or "")
    if not external_userid:
        return
    match change:
        case "add_external_contact" | "edit_external_contact" | "add_half_external_contact":
            await refresh_contact(ctx, tenant_id, external_userid)
            welcome_code = event.get("WelcomeCode")
            if change == "add_external_contact" and welcome_code:
                await send_welcome(ctx, tenant_id, str(welcome_code))
        case "del_external_contact" | "del_follow_user":
            async with ctx.db.tenant_session(tenant_id) as session:
                await session.execute(
                    update(WecomContactFollow)
                    .where(
                        WecomContactFollow.external_userid == external_userid,
                        WecomContactFollow.userid == userid,
                    )
                    .values(deleted_at=utcnow())
                )
                await session.commit()
        case "transfer_fail":
            reason = (
                "客户拒绝了转接"
                if event.get("FailReason") == "customer_refused"
                else ("接替成员的客户数已达上限")
            )
            async with ctx.db.tenant_session(tenant_id) as session:
                await _finish_transfers(
                    session,
                    external_userid,
                    takeover_userid=userid,
                    status=TransferStatus.FAILED,
                    error=reason,
                )
                await session.commit()
        case _:
            pass


# ---- 欢迎语 ----


async def send_welcome(ctx: AppContext, tenant_id: UUID, welcome_code: str) -> bool:
    """新客户欢迎语，可以附带微信客服链接（AI 客服入口）。welcome_code 20 秒内有效。"""
    wecom = client_of(ctx)
    async with ctx.db.tenant_session(tenant_id) as session:
        corp = await active_corp(session)
        if corp is None:
            return False
        settings = WecomSettings.of(corp.settings)
        if not settings.welcome_enabled:
            return False
        link = None
        if settings.welcome_kf_id:
            link = await session.scalar(
                select(WecomKfAccount.contact_url).where(
                    WecomKfAccount.open_kfid == settings.welcome_kf_id
                )
            )
    body: dict[str, Any] = {
        "welcome_code": welcome_code,
        "text": {"content": settings.welcome_text},
    }
    if link:
        body["attachments"] = [
            {
                "msgtype": "link",
                "link": {"title": WELCOME_LINK_TITLE, "desc": WELCOME_LINK_DESC, "url": link},
            }
        ]
    try:
        await wecom.corp_call(
            corp.corp_id, "POST", "/cgi-bin/externalcontact/send_welcome_msg", json=body
        )
    except WeComError as exc:
        logger.warning("send_welcome_msg failed: %s", exc)
        return False
    return True


# ---- 标签写回 ----


async def write_back_tags(
    ctx: AppContext, tenant_id: UUID, customer_id: UUID, added: set[str], removed: set[str]
) -> None:
    """平台上给客户增删的标签（企业标签里有的）写回企业微信：新增的打在归属坐席（或最早的添加人）
    的关系上，删除的从所有添加人的关系上去掉。"""
    if ctx.wecom is None or not (added or removed):
        return
    async with ctx.db.tenant_session(tenant_id) as session:
        corp = await active_corp(session)
        if corp is None or not WecomSettings.of(corp.settings).tag_writeback:
            return
        by_name = {name: tag_id for tag_id, name in (await _tag_names(session)).items()}
        add_ids = {by_name[n] for n in added if n in by_name}
        remove_ids = {by_name[n] for n in removed if n in by_name}
        if not (add_ids or remove_ids):
            return
        follows = (
            await session.scalars(
                select(WecomContactFollow)
                .where(
                    WecomContactFollow.customer_id == customer_id,
                    WecomContactFollow.deleted_at.is_(None),
                )
                .order_by(WecomContactFollow.added_at.nulls_last())
            )
        ).all()
        if not follows:
            return
        customer = await session.get(Customer, customer_id)
        owner_userid = None
        if customer is not None and customer.owner_id is not None:
            owner_userid = await session.scalar(
                select(Staff.wecom_userid).where(Staff.id == customer.owner_id)
            )
        primary = next((f for f in follows if f.userid == owner_userid), follows[0])
        plan: list[tuple[WecomContactFollow, list[str], list[str]]] = []
        for follow in follows:
            add = sorted(add_ids - set(follow.tag_ids)) if follow is primary else []
            remove = sorted(remove_ids & set(follow.tag_ids))
            if add or remove:
                plan.append((follow, add, remove))
        corp_id = corp.corp_id
    for follow, add, remove in plan:
        try:
            await ctx.wecom.corp_call(
                corp_id,
                "POST",
                "/cgi-bin/externalcontact/mark_tag",
                json={
                    "userid": follow.userid,
                    "external_userid": follow.external_userid,
                    "add_tag": add,
                    "remove_tag": remove,
                },
            )
        except WeComError as exc:
            logger.warning("mark_tag for %s failed: %s", follow.external_userid, exc)
            continue
        async with ctx.db.tenant_session(tenant_id) as session:
            row = await session.get(WecomContactFollow, follow.id)
            if row is not None:
                row.tag_ids = sorted((set(row.tag_ids) | set(add)) - set(remove))
            await session.commit()


# ---- 客户群 ----


async def sync_groups(ctx: AppContext, tenant_id: UUID) -> int:
    wecom = client_of(ctx)
    loaded = await _load(ctx, tenant_id)
    if loaded is None:
        return 0
    corp_id = loaded.corp.corp_id
    chat_ids: list[str] = []
    cursor = ""
    while True:
        data = await wecom.corp_call(
            corp_id,
            "POST",
            "/cgi-bin/externalcontact/groupchat/list",
            json={"status_filter": 0, "cursor": cursor, "limit": 1000},
        )
        chat_ids += [
            str(c["chat_id"]) for c in data.get("group_chat_list") or [] if c.get("chat_id")
        ]
        cursor = str(data.get("next_cursor") or "")
        if not cursor:
            break
    for chat_id in chat_ids:
        try:
            await refresh_group(ctx, tenant_id, chat_id)
        except WeComError as exc:
            logger.warning("groupchat/get %s failed: %s", chat_id, exc)
    async with ctx.db.tenant_session(tenant_id) as session:
        await session.execute(
            update(WecomGroupChat)
            .where(
                WecomGroupChat.chat_id.not_in(chat_ids),
                WecomGroupChat.status == GroupChatStatus.NORMAL,
            )
            .values(status=GroupChatStatus.DISMISSED)
        )
        await session.commit()
    await record_sync(ctx, tenant_id, "groups", count=len(chat_ids))
    return len(chat_ids)


async def refresh_group(ctx: AppContext, tenant_id: UUID, chat_id: str) -> None:
    wecom = client_of(ctx)
    loaded = await _load(ctx, tenant_id)
    if loaded is None:
        return
    data = await wecom.corp_call(
        loaded.corp.corp_id,
        "POST",
        "/cgi-bin/externalcontact/groupchat/get",
        json={"chat_id": chat_id, "need_name": 1},
    )
    chat = data.get("group_chat") or {}
    members = chat.get("member_list") or []
    now = utcnow()
    async with ctx.db.tenant_session(tenant_id) as session:
        values = {
            "name": str(chat.get("name") or "")[:128],
            "owner_userid": chat.get("owner"),
            "notice": chat.get("notice") or None,
            "member_count": len(members),
            "status": GroupChatStatus.NORMAL,
            "created_time": _ts(chat.get("create_time")),
            "synced_at": now,
            "updated_at": now,
        }
        await session.execute(
            insert(WecomGroupChat)
            .values(id=new_id(), tenant_id=tenant_id, chat_id=chat_id, **values)
            .on_conflict_do_update(constraint="uq_wecom_group_chats", set_=values)
        )
        await session.execute(delete(WecomGroupMember).where(WecomGroupMember.chat_id == chat_id))
        externals = [str(m["userid"]) for m in members if m.get("type") == GroupMemberType.EXTERNAL]
        customers = dict(
            (
                await session.execute(
                    select(CustomerIdentity.external_id, CustomerIdentity.customer_id)
                    .join(
                        ChannelAccount,
                        ChannelAccount.id == CustomerIdentity.channel_account_id,
                    )
                    .where(
                        CustomerIdentity.external_id.in_(externals),
                        ChannelAccount.type.in_([ChannelType.WECOM_CONTACT, ChannelType.WECOM_KF]),
                    )
                )
            ).all()
        )
        for member in members:
            member_id = str(member.get("userid") or "")
            if not member_id:
                continue
            member_type = int(member.get("type") or GroupMemberType.MEMBER)
            session.add(
                WecomGroupMember(
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    member_id=member_id,
                    type=member_type,
                    name=(str(member.get("name") or "")[:128] or None),
                    customer_id=customers.get(member_id),
                    join_time=_ts(member.get("join_time")),
                    join_scene=member.get("join_scene"),
                    state=(str(member.get("state") or "")[:64] or None),
                )
            )
        await session.commit()


async def on_chat_change(ctx: AppContext, tenant_id: UUID, event: dict[str, Any]) -> None:
    chat_id = str(event.get("ChatId") or "")
    if not chat_id:
        return
    if event.get("ChangeType") == "dismiss":
        async with ctx.db.tenant_session(tenant_id) as session:
            await session.execute(
                update(WecomGroupChat)
                .where(WecomGroupChat.chat_id == chat_id)
                .values(status=GroupChatStatus.DISMISSED, member_count=0)
            )
            await session.execute(
                delete(WecomGroupMember).where(WecomGroupMember.chat_id == chat_id)
            )
            await session.commit()
        return
    await refresh_group(ctx, tenant_id, chat_id)


# ---- 客户继承（在职继承、离职继承） ----


@dataclass(frozen=True)
class OwnerChange:
    customer_id: UUID
    from_owner_id: UUID | None
    to_owner_id: UUID | None
    history_id: UUID | None


@dataclass
class TransferSummary:
    requested: int = 0
    skipped: int = 0
    failed: int = 0
    resigned: int = 0


@dataclass(frozen=True)
class TransferItem:
    customer_id: UUID
    external_userid: str
    history_id: UUID | None


async def left_members(session: AsyncSession) -> set[str]:
    """已经离职（从通讯录删除）的企业成员。"""
    rows = await session.scalars(
        select(WecomMember.userid).where(WecomMember.status == MemberStatus.LEFT)
    )
    return set(rows.all())


async def transfer_owner_changes(
    ctx: AppContext, tenant_id: UUID, actor_id: UUID | None, changes: list[OwnerChange]
) -> TransferSummary:
    """把平台上的客户转移同步到企业微信：原添加人在职时在职继承，已离职时离职继承。
    原归属坐席或新归属坐席没有绑定企业微信成员、客户不是原归属坐席添加的，都跳过。"""
    summary = TransferSummary()
    if ctx.wecom is None or not changes:
        summary.skipped = len(changes)
        return summary
    async with ctx.db.tenant_session(tenant_id) as session:
        corp = await active_corp(session)
        if corp is None:
            summary.skipped = len(changes)
            return summary
        corp_id = corp.corp_id
        staff_ids = {s for c in changes for s in (c.from_owner_id, c.to_owner_id) if s}
        userids = dict(
            (
                await session.execute(
                    select(Staff.id, Staff.wecom_userid).where(
                        Staff.id.in_(staff_ids), Staff.wecom_userid.is_not(None)
                    )
                )
            ).all()
        )
        follows = (
            await session.execute(
                select(
                    WecomContactFollow.customer_id,
                    WecomContactFollow.userid,
                    WecomContactFollow.external_userid,
                ).where(
                    WecomContactFollow.customer_id.in_({c.customer_id for c in changes}),
                    WecomContactFollow.deleted_at.is_(None),
                )
            )
        ).all()
        left = await left_members(session)
    by_customer = {(customer_id, userid): external for customer_id, userid, external in follows}
    groups: dict[tuple[str, str], list[TransferItem]] = defaultdict(list)
    for change in changes:
        handover = userids.get(change.from_owner_id) if change.from_owner_id else None
        takeover = userids.get(change.to_owner_id) if change.to_owner_id else None
        external = by_customer.get((change.customer_id, handover or ""))
        if not handover or not takeover or handover == takeover or external is None:
            summary.skipped += 1
            continue
        groups[(handover, takeover)].append(
            TransferItem(change.customer_id, external, change.history_id)
        )
    for (handover, takeover), items in groups.items():
        kind = TransferKind.RESIGNED if handover in left else TransferKind.ONJOB
        part = await submit_transfers(
            ctx, tenant_id, corp_id, actor_id, handover, takeover, kind, items
        )
        summary.requested += part.requested
        summary.failed += part.failed
        if kind == TransferKind.RESIGNED:
            summary.resigned += part.requested
    return summary


async def submit_transfers(
    ctx: AppContext,
    tenant_id: UUID,
    corp_id: str,
    actor_id: UUID | None,
    handover: str,
    takeover: str,
    kind: TransferKind,
    items: list[TransferItem],
) -> TransferSummary:
    """提交一批客户继承（每次最多 100 位），记录每位客户的继承状态，结果由 poll_transfers 回收。"""
    summary = TransferSummary()
    if ctx.wecom is None or not items:
        return summary
    path = _TRANSFER_PATHS[kind][0]
    rows: list[WecomTransfer] = []
    for start in range(0, len(items), _TRANSFER_BATCH):
        chunk = items[start : start + _TRANSFER_BATCH]
        results: dict[str, int] = {}
        error: str | None = None
        try:
            data = await ctx.wecom.corp_call(
                corp_id,
                "POST",
                path,
                json={
                    "handover_userid": handover,
                    "takeover_userid": takeover,
                    "external_userid": [item.external_userid for item in chunk],
                },
            )
            results = {
                str(r.get("external_userid")): int(r.get("errcode") or 0)
                for r in data.get("customer") or []
            }
        except WeComError as exc:
            error = f"{exc.errmsg or '企业微信拒绝转接'}（{exc.errcode}）"
            results = {item.external_userid: exc.errcode or -1 for item in chunk}
        for item in chunk:
            errcode = results.get(item.external_userid, 0)
            ok = errcode == 0
            summary.requested += ok
            summary.failed += not ok
            rows.append(
                WecomTransfer(
                    tenant_id=tenant_id,
                    customer_id=item.customer_id,
                    history_id=item.history_id,
                    external_userid=item.external_userid,
                    handover_userid=handover,
                    takeover_userid=takeover,
                    kind=kind,
                    status=TransferStatus.WAITING if ok else TransferStatus.FAILED,
                    errcode=None if ok else errcode,
                    error=None if ok else (error or _transfer_error(errcode, kind)),
                    created_by=actor_id,
                )
            )
    async with ctx.db.tenant_session(tenant_id) as session:
        session.add_all(rows)
        for row in rows:
            if row.history_id is not None:
                await session.execute(
                    update(CustomerOwnerHistory)
                    .where(CustomerOwnerHistory.id == row.history_id)
                    .values(wecom_sync_status=row.status)
                )
        await session.commit()
    return summary


def _transfer_error(errcode: int, kind: str = TransferKind.ONJOB) -> str:
    if kind == TransferKind.RESIGNED:
        return f"企业微信拒绝离职继承（错误码 {errcode}）：客户可能已经分配过或已不是好友"
    return (
        f"企业微信拒绝转接（错误码 {errcode}）："
        "90 天内每位客户最多转接 2 次，转接中的客户不能再次转接"
    )


async def _finish_transfers(
    session: AsyncSession,
    external_userid: str,
    *,
    takeover_userid: str,
    status: str,
    error: str | None = None,
    takeover_at: datetime | None = None,
) -> None:
    transfers = (
        await session.scalars(
            select(WecomTransfer).where(
                WecomTransfer.external_userid == external_userid,
                WecomTransfer.takeover_userid == takeover_userid,
                WecomTransfer.status == TransferStatus.WAITING,
            )
        )
    ).all()
    for transfer in transfers:
        transfer.status = status
        transfer.error = error
        transfer.takeover_at = takeover_at
        if transfer.history_id is not None:
            await session.execute(
                update(CustomerOwnerHistory)
                .where(CustomerOwnerHistory.id == transfer.history_id)
                .values(wecom_sync_status=status)
            )
        if status == TransferStatus.SUCCESS:
            await session.execute(
                update(WecomContactFollow)
                .where(
                    WecomContactFollow.external_userid == external_userid,
                    WecomContactFollow.userid == transfer.handover_userid,
                )
                .values(deleted_at=takeover_at or utcnow())
            )


async def poll_transfers(ctx: AppContext) -> int:
    """回收在职继承、离职继承的结果（调度进程每小时）。返回有结果的数量。"""
    if ctx.wecom is None:
        return 0
    async with ctx.db.platform_sessionmaker() as session:
        pairs = (
            await session.execute(
                select(
                    WecomTransfer.tenant_id,
                    WecomTransfer.handover_userid,
                    WecomTransfer.takeover_userid,
                    WecomTransfer.kind,
                )
                .where(WecomTransfer.status == TransferStatus.WAITING)
                .distinct()
            )
        ).all()
    finished = 0
    for tenant_id, handover, takeover, kind in pairs:
        loaded = await _load(ctx, tenant_id)
        if loaded is None:
            continue
        cursor = ""
        results: list[dict[str, Any]] = []
        try:
            while True:
                data = await ctx.wecom.corp_call(
                    loaded.corp.corp_id,
                    "POST",
                    _TRANSFER_PATHS[TransferKind(kind)][1],
                    json={
                        "handover_userid": handover,
                        "takeover_userid": takeover,
                        "cursor": cursor,
                    },
                )
                results += data.get("customer") or []
                cursor = str(data.get("next_cursor") or "")
                if not cursor:
                    break
        except WeComError as exc:
            logger.warning("transfer_result %s→%s failed: %s", handover, takeover, exc)
            continue
        takeovers: list[str] = []
        async with ctx.db.tenant_session(tenant_id) as session:
            for result in results:
                status, error = _TRANSFER_RESULTS.get(int(result.get("status") or 0), (None, None))
                external = str(result.get("external_userid") or "")
                if status is None or status == TransferStatus.WAITING or not external:
                    continue
                await _finish_transfers(
                    session,
                    external,
                    takeover_userid=takeover,
                    status=status,
                    error=error,
                    takeover_at=_ts(result.get("takeover_time")),
                )
                finished += 1
                if status == TransferStatus.SUCCESS:
                    takeovers.append(external)
            await session.commit()
        for external in takeovers:
            try:
                await refresh_contact(ctx, tenant_id, external)
            except WeComError as exc:
                logger.info("refresh contact %s after transfer failed: %s", external, exc)
    return finished
