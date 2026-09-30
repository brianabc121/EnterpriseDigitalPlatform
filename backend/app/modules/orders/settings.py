"""订单设置（租户设置 tenant_settings.orders，设计文档 §25.9）。没有保存过时按默认值。

订单的处理人按"订单审核"待办类型的分派规则确定（在"设置 → 待办"里修改）。
"""

import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.security.models import TenantSetting

ReceiverField = Literal["receiver_name", "receiver_phone", "receiver_address", "expected_at"]
PaymentMethodValue = Literal["online", "cod", "deposit", "credit"]
DEFAULT_REQUIRED: list[ReceiverField] = ["receiver_name", "receiver_phone", "receiver_address"]
ALL_METHODS: list[PaymentMethodValue] = ["online", "cod", "deposit", "credit"]


class OrderSettings(BaseModel):
    prefix: str = Field(
        default="SO", pattern=r"^[A-Z]{1,6}$", description="订单号前缀（1 到 6 个大写字母）"
    )
    required_fields: list[ReceiverField] = Field(
        default_factory=lambda: list(DEFAULT_REQUIRED),
        description="提交订单时必须有的信息（AI 会先向客户追问）",
    )
    payment_methods: list[PaymentMethodValue] = Field(
        default_factory=lambda: list(ALL_METHODS),
        min_length=1,
        description="启用的收款方式",
    )
    deposit_balance: Literal["before_ship", "on_delivery"] = Field(
        default="before_ship", description="预付定金的尾款：发货前收清，或货到时收取"
    )
    shipping_enabled: bool = Field(
        default=True, description="有发货环节（服务类订单可以关闭：处理中直接完成）"
    )
    ai_price_enabled: bool = Field(
        default=True, description="AI 可以告诉客户商品的建议零售价（永远不会告诉成本价）"
    )
    ai_order_mode: Literal["off", "collect"] = Field(
        default="collect", description="AI 下单：off 关闭；collect 采集信息并提交审核"
    )
    ai_daily_limit: int = Field(
        default=3, ge=1, le=20, description="每位客户每天最多由 AI 提交几个订单"
    )
    draft_followup: bool = Field(
        default=False,
        description="客户中途离开、AI 采集的订单草稿没有提交时，生成一条「跟进未完成的订单」待办",
    )
    draft_followup_minutes: int = Field(
        default=60, ge=10, le=1440, description="草稿多久没有更新算作客户已离开（分钟）"
    )
    max_quantity: int = Field(default=999, ge=1, le=100_000, description="每个商品行的数量上限")
    discount_limit: int = Field(
        default=30,
        ge=0,
        le=100,
        description="优惠上限（相对建议零售价的折扣百分比）；超过时需要有 order:credit 权限的"
        "主管操作",
    )
    tracking_days: int = Field(
        default=90, ge=7, le=3650, description="订单完成或取消后，跟踪链接保留的天数"
    )
    erase_mode: Literal["anonymize", "delete"] = Field(
        default="anonymize",
        description="客户申请删除个人信息时：anonymize 清空订单里的个人信息（保留商品、金额和收款"
        "用于统计）；delete 整单删除",
    )
    promise_text: str = Field(
        default="订单已提交，编号 {no}，客服核对后会尽快联系您确认。",
        max_length=500,
        description="AI 提交订单后答复客户的话术（{no} 为订单号）",
    )
    confirm_template: str = Field(
        default="您好，您的订单 {no} 已确认：{summary}，合计 {total} 元，收款方式：{payment}。"
        "查看订单进度：{link}",
        max_length=1000,
    )
    ship_template: str = Field(
        default="您好，您的订单 {no} 已发货：{company} {tracking_no}。查看订单进度：{link}",
        max_length=1000,
    )
    complete_template: str = Field(
        default="您好，您的订单 {no} 已完成，感谢您的支持。", max_length=1000
    )
    cancel_template: str = Field(default="您好，您的订单 {no} 已取消：{reason}", max_length=1000)
    update_template: str = Field(
        default="您好，您的订单 {no} 已更新：{summary}，合计 {total} 元。查看订单进度：{link}",
        max_length=1000,
    )

    @field_validator("required_fields", "payment_methods")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


async def load(session: AsyncSession, tenant_id: uuid.UUID) -> OrderSettings:
    row = await session.get(TenantSetting, tenant_id)
    return OrderSettings.model_validate((row.orders if row is not None else None) or {})


async def save(
    session: AsyncSession, tenant_id: uuid.UUID, value: OrderSettings, staff_id: uuid.UUID
) -> OrderSettings:
    """保存（由调用方提交）。"""
    row = await session.get(TenantSetting, tenant_id, with_for_update=True)
    if row is None:
        row = TenantSetting(tenant_id=tenant_id)
        session.add(row)
    row.orders = value.model_dump()
    row.updated_by = staff_id
    return value
