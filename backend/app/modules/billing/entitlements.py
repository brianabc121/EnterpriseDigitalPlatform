"""套餐权益：租户当前可用的额度与功能（设计文档 §7.3）。

当前套餐是最近创建的那条订阅所属的套餐（订阅到期后宽限期内仍按它计算，宽限期后租户被停用）。
从来没有订阅的租户（启用计费之前开通的租户）不限额、功能全开。

运营可以按租户覆盖套餐：租户设置里的 limits（键不存在表示按套餐，值为 null 表示不限）和
features。早期的 ai_monthly_quota 设置仍然有效，作为每月 AI 回复条数的覆盖值。
"""

import calendar
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dates import day_bounds
from app.core.errors import AppError
from app.modules.ai.models import AiDecision, DecisionAction
from app.modules.billing.models import Plan, Subscription
from app.modules.channels.models import ChannelAccount, ChannelStatus
from app.modules.iam.models import Staff, StaffStatus
from app.modules.kb.models import ItemStatus, KbItem
from app.modules.tenancy.models import Tenant

LimitKey = Literal["seats", "ai_replies_monthly", "kb_items", "channels"]
FeatureKey = Literal["ai", "wecom", "broadcast", "extraction", "zone", "todos", "orders"]

# 额度：名称与单位。
LIMITS: dict[str, tuple[str, str]] = {
    "seats": ("坐席账号", "个"),
    "ai_replies_monthly": ("每月 AI 回复", "条"),
    "kb_items": ("知识条目", "条"),
    "channels": ("接入渠道", "个"),
}
FEATURES: dict[str, str] = {
    "ai": "AI 接待与坐席助手",
    "wecom": "企业微信接入",
    "broadcast": "企业微信群发",
    "extraction": "聊天知识提炼",
    "zone": "数据与智能专区",
    "todos": "待办（AI 登记、会话后解析）",
    "orders": "订单与商品库（AI 下单、收款登记、订单跟踪）",
}
UPGRADE_HINT = "请联系平台升级套餐"


class PlanLimitReached(AppError):
    status_code = 409
    code = "plan_limit"


class PlanFeatureMissing(AppError):
    status_code = 403
    code = "plan_feature"


@dataclass(frozen=True)
class Entitlements:
    plan: Plan | None
    subscription: Subscription | None
    limits: dict[str, int | None]
    features: dict[str, bool]
    overage: dict[str, Any] = field(default_factory=dict)
    overrides: dict[str, Any] = field(default_factory=dict)

    def has(self, feature: str) -> bool:
        return self.features.get(feature, True)

    def limit(self, key: str) -> int | None:
        return self.limits.get(key)

    @property
    def overage_allowed(self) -> bool:
        """超出 AI 额度后继续回复并计费（套餐的 overage.policy 为 warn）。"""
        return self.overage.get("policy") == "warn"

    @property
    def ai_reply_price(self) -> int:
        return _count(self.overage.get("ai_reply_price")) or 0


def _count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def overrides_of(settings: dict[str, Any] | None) -> dict[str, Any]:
    """租户设置里的覆盖值：{"limits": {...}, "features": {...}}。"""
    settings = settings or {}
    limits = {k: _count(v) for k, v in (settings.get("limits") or {}).items() if k in LIMITS}
    if "ai_monthly_quota" in settings and "ai_replies_monthly" not in limits:
        limits["ai_replies_monthly"] = _count(settings["ai_monthly_quota"])
    features = {k: bool(v) for k, v in (settings.get("features") or {}).items() if k in FEATURES}
    return {"limits": limits, "features": features}


def resolve(
    plan: Plan | None,
    subscription: Subscription | None,
    settings: dict[str, Any] | None,
    *,
    closing: bool = False,
) -> Entitlements:
    limits: dict[str, int | None] = dict.fromkeys(LIMITS)
    features = dict.fromkeys(FEATURES, True)
    overage: dict[str, Any] = {"policy": "degrade"}
    if plan is not None:
        for key in LIMITS:
            limits[key] = _count((plan.limits or {}).get(key))
        for key in FEATURES:
            features[key] = bool((plan.features or {}).get(key, True))
        overage.update(plan.overage or {})
    overrides = overrides_of(settings)
    limits.update(overrides["limits"])
    features.update(overrides["features"])
    if closing:
        # 申请注销后的保留期内停止 AI、企业微信、群发等功能（员工仍可登录导出数据）。
        features = dict.fromkeys(FEATURES, False)
    return Entitlements(
        plan=plan,
        subscription=subscription,
        limits=limits,
        features=features,
        overage=overage,
        overrides=overrides,
    )


async def latest_subscription(session: AsyncSession, tenant_id: uuid.UUID) -> Subscription | None:
    return await session.scalar(
        select(Subscription)
        .where(Subscription.tenant_id == tenant_id)
        .order_by(Subscription.created_at.desc(), Subscription.id.desc())
        .limit(1)
    )


async def entitlements(session: AsyncSession, tenant_id: uuid.UUID) -> Entitlements:
    row = (
        await session.execute(
            select(Tenant.settings, Tenant.closing_requested_at).where(Tenant.id == tenant_id)
        )
    ).first()
    settings, closing_at = row if row else (None, None)
    subscription = await latest_subscription(session, tenant_id)
    plan = await session.get(Plan, subscription.plan_id) if subscription else None
    return resolve(plan, subscription, settings, closing=closing_at is not None)


# ---- 用量 ----


def month_start(day: date) -> date:
    return day.replace(day=1)


def month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def add_months(day: date, months: int) -> date:
    """加 months 个月，月底对齐（1 月 31 日加一个月是 2 月最后一天）。"""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


async def ai_replies(
    session: AsyncSession, tenant_id: uuid.UUID, start: datetime, end: datetime | None = None
) -> int:
    """AI 回复条数（按判定为回复的次数计）。"""
    statement = select(func.count()).where(
        AiDecision.tenant_id == tenant_id,
        AiDecision.action == DecisionAction.REPLY,
        AiDecision.created_at >= start,
    )
    if end is not None:
        statement = statement.where(AiDecision.created_at < end)
    return int(await session.scalar(statement) or 0)


async def ai_replies_this_month(
    session: AsyncSession, tenant_id: uuid.UUID, tz: ZoneInfo, now: datetime | None = None
) -> int:
    local = (now or datetime.now(UTC)).astimezone(tz).date()
    return await ai_replies(session, tenant_id, day_bounds(month_start(local), tz)[0])


async def used(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    key: str,
    tz: ZoneInfo,
    now: datetime | None = None,
) -> int:
    """某项额度的当前用量。"""
    if key == "seats":
        statement = select(func.count()).where(
            Staff.tenant_id == tenant_id, Staff.status == StaffStatus.ACTIVE
        )
    elif key == "channels":
        statement = select(func.count()).where(
            ChannelAccount.tenant_id == tenant_id, ChannelAccount.status == ChannelStatus.ACTIVE
        )
    elif key == "kb_items":
        statement = select(func.count()).where(
            KbItem.tenant_id == tenant_id, KbItem.status != ItemStatus.ARCHIVED
        )
    elif key == "ai_replies_monthly":
        return await ai_replies_this_month(session, tenant_id, tz, now)
    else:
        raise ValueError(f"unknown limit: {key}")
    return int(await session.scalar(statement) or 0)


# ---- 校验 ----


async def check_limit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    key: LimitKey,
    *,
    adding: int = 1,
    tz: ZoneInfo | None = None,
) -> None:
    """再增加 adding 个是否超出套餐额度；超出时拒绝（409 plan_limit）。"""
    limit = (await entitlements(session, tenant_id)).limit(key)
    if limit is None:
        return
    current = await used(session, tenant_id, key, tz or ZoneInfo("UTC"))
    if current + adding > limit:
        label, unit = LIMITS[key]
        raise PlanLimitReached(f"已达到套餐的{label}上限（{limit} {unit}），{UPGRADE_HINT}")


async def has_feature(session: AsyncSession, tenant_id: uuid.UUID, feature: FeatureKey) -> bool:
    return (await entitlements(session, tenant_id)).has(feature)


async def require_feature(session: AsyncSession, tenant_id: uuid.UUID, feature: FeatureKey) -> None:
    if not await has_feature(session, tenant_id, feature):
        raise PlanFeatureMissing(f"当前套餐不包含{FEATURES[feature]}，{UPGRADE_HINT}")
