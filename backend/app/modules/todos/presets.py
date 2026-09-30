"""预置的待办类型（设计文档 §24.2，2026-09-30 确认）。租户可以修改或停用，不能删除。

新开通的租户在开通时创建；已有的租户在第一次用到时补齐（ensure_presets，并发安全）。
预置的技能组（财务、售前、售后、投诉）要由租户在类型设置里指定，没有指定时进入待认领池。
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.modules.todos.models import FieldType, Priority, TodoType

LEAVE_MESSAGE = "leave_message"
ORDER_REVIEW = "order_review"
COLLECTION = "collection"
COMPLAINT = "complaint"
OTHER = "other"


def _field(
    key: str,
    label: str,
    type_: FieldType = FieldType.TEXT,
    *,
    required: bool = False,
    sensitive: bool = False,
    options: list[str] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "key": key,
        "label": label,
        "type": type_.value,
        "required": required,
        "sensitive": sensitive,
    }
    if options:
        spec["options"] = options
    return spec


def _rule(*steps: str, group_mode: str = "pool") -> dict[str, Any]:
    return {"steps": list(steps), "group_mode": group_mode}


@dataclass(frozen=True)
class Preset:
    code: str
    name: str
    ai_hint: str
    examples: tuple[str, ...]
    fields: tuple[dict[str, Any], ...]
    assign_rule: dict[str, Any]
    promise_text: str
    done_template: str
    priority: Priority = Priority.NORMAL
    sla_response_minutes: int | None = None
    sla_resolve_minutes: int | None = None
    sla_resolve_days: int | None = None
    ai_enabled: bool = True
    handoff: bool = False
    notify_supervisor: bool = False
    system: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def values(self, tenant_id: uuid.UUID, sort: int) -> dict[str, Any]:
        return {
            "id": new_id(),
            "tenant_id": tenant_id,
            "code": self.code,
            "name": self.name,
            "ai_hint": self.ai_hint,
            "examples": list(self.examples),
            "fields": list(self.fields),
            "assign_rule": self.assign_rule,
            "priority": self.priority.value,
            "sla_response_minutes": self.sla_response_minutes,
            "sla_resolve_minutes": self.sla_resolve_minutes,
            "sla_resolve_days": self.sla_resolve_days,
            "ai_enabled": self.ai_enabled and not self.system,
            "handoff": self.handoff,
            "notify_supervisor": self.notify_supervisor,
            "promise_text": self.promise_text,
            "done_template": self.done_template,
            "preset": True,
            "system": self.system,
            "sort": sort,
        }


PRESETS: tuple[Preset, ...] = (
    Preset(
        code="callback",
        name="回电 / 回访",
        ai_hint="客户希望客服稍后给他打电话或回访，例如晚点回电、明天再联系。",
        examples=("晚点给我回个电话", "明天上午打我电话吧"),
        fields=(
            _field("phone", "回电号码", FieldType.PHONE, sensitive=True),
            _field("best_time", "方便接听的时间"),
        ),
        assign_rule=_rule("session_agent", "owner"),
        sla_resolve_minutes=240,
        promise_text="已为您登记回电，客服确认后会尽快与您联系。",
        done_template="您好，关于您的回电需求：{result}",
    ),
    Preset(
        code=LEAVE_MESSAGE,
        name="留言",
        ai_hint="客户留下需要稍后联系或处理的问题，例如非工作时间、客服不在线时的留言。",
        examples=("现在没人吗，有空回我一下", "麻烦明天联系我"),
        fields=(_field("contact", "联系方式"),),
        assign_rule=_rule("owner", "channel_group"),
        sla_resolve_days=1,
        promise_text="已为您记录留言，客服确认后会尽快联系您。",
        done_template="您好，您的留言我们已经处理：{result}",
    ),
    Preset(
        code="send_materials",
        name="资料寄送",
        ai_hint="客户要求发送产品手册、报价单、合同等资料到邮箱或寄送到地址。",
        examples=("把产品手册发我邮箱", "能寄一份纸质资料给我吗"),
        fields=(
            _field("material", "资料名称", required=True),
            _field("email", "接收邮箱", FieldType.EMAIL),
            _field("address", "寄送地址", FieldType.ADDRESS, sensitive=True),
        ),
        assign_rule=_rule("session_agent", "owner"),
        sla_resolve_minutes=240,
        promise_text="已为您登记资料寄送，客服确认后会尽快发给您。",
        done_template="您好，您要的资料已经发出：{result}",
    ),
    Preset(
        code="invoice",
        name="开票",
        ai_hint="客户要求开具发票（普通发票或增值税专用发票）。需要发票类型、抬头和税号。",
        examples=("帮我开一张专票", "上个月的订单还没开发票"),
        fields=(
            _field(
                "invoice_type",
                "发票类型",
                FieldType.OPTION,
                required=True,
                options=["普通发票", "增值税专用发票"],
            ),
            _field("invoice_title", "发票抬头", required=True),
            _field("tax_no", "税号", required=True),
            _field("email", "接收邮箱", FieldType.EMAIL),
            _field("order_no", "订单号"),
            _field("amount", "开票金额", FieldType.NUMBER),
        ),
        assign_rule=_rule("skill_group"),
        sla_resolve_days=1,
        promise_text="已为您记录开票申请，客服确认后会尽快为您处理。",
        done_template="您好，您的发票已开具：{result}",
    ),
    Preset(
        code="quote",
        name="报价",
        ai_hint="客户询问批量采购、定制或优惠价格，需要销售报价。",
        examples=("100 台的话什么价", "能不能给个批量价"),
        fields=(
            _field("product", "产品或型号", required=True),
            _field("quantity", "数量", FieldType.NUMBER),
        ),
        assign_rule=_rule("owner", "skill_group"),
        sla_resolve_minutes=240,
        promise_text="已为您登记报价需求，客服确认后会尽快联系您。",
        done_template="您好，您要的报价：{result}",
    ),
    Preset(
        code="visit",
        name="预约上门",
        ai_hint="客户预约上门安装、测量、维修等服务，需要地址和联系电话，时间以客户期望为准。",
        examples=("周六上午来装", "什么时候能上门测量"),
        fields=(
            _field("address", "上门地址", FieldType.ADDRESS, required=True, sensitive=True),
            _field("phone", "联系电话", FieldType.PHONE, required=True, sensitive=True),
        ),
        assign_rule=_rule("owner"),
        promise_text="已为您登记上门预约，客服确认时间后会联系您。",
        done_template="您好，上门服务已安排：{result}",
    ),
    Preset(
        code="after_sales",
        name="退换货 / 售后维修",
        ai_hint="客户收到的商品有问题，要求退货、换货或维修。",
        examples=("收到的有破损，要换一个", "用了两天坏了能修吗"),
        fields=(
            _field(
                "request", "诉求", FieldType.OPTION, required=True, options=["退货", "换货", "维修"]
            ),
            _field("product", "商品"),
            _field("order_no", "订单号"),
        ),
        assign_rule=_rule("skill_group"),
        sla_resolve_days=1,
        promise_text="已为您登记售后申请，客服确认后会尽快为您处理。",
        done_template="您好，您的售后申请已处理：{result}",
    ),
    Preset(
        code=COMPLAINT,
        name="投诉处理",
        ai_hint="客户投诉服务或产品、要求赔偿或表达强烈不满。同时转人工。",
        examples=("我要投诉", "你们必须给我赔偿"),
        fields=(_field("order_no", "订单号"), _field("demand", "客户诉求")),
        assign_rule=_rule("skill_group"),
        priority=Priority.HIGH,
        sla_response_minutes=60,
        sla_resolve_days=2,
        handoff=True,
        notify_supervisor=True,
        promise_text="非常抱歉给您带来不好的体验，已为您登记，客服会尽快联系您处理。",
        done_template="您好，关于您反馈的问题：{result}",
    ),
    Preset(
        code=ORDER_REVIEW,
        name="订单审核",
        ai_hint="订单提交后由系统生成。",
        examples=(),
        fields=(),
        assign_rule=_rule("owner"),
        sla_resolve_minutes=120,
        system=True,
        promise_text="",
        done_template="",
    ),
    Preset(
        code=COLLECTION,
        name="催收",
        ai_hint="暂欠订单到期未收清时由系统生成。",
        examples=(),
        fields=(),
        assign_rule=_rule("owner"),
        sla_resolve_days=1,
        system=True,
        promise_text="",
        done_template="",
    ),
    Preset(
        code=OTHER,
        name="其他",
        ai_hint="客户提出的、需要员工线下处理但不属于以上类型的事。",
        examples=("帮我查一下上次的维修记录",),
        fields=(),
        assign_rule=_rule("owner"),
        sla_resolve_days=1,
        promise_text="已为您记录，客服确认后会尽快为您处理。",
        done_template="您好，您的需求已处理：{result}",
    ),
)
PRESET_CODES = tuple(p.code for p in PRESETS)


def preset_rows(tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    return [p.values(tenant_id, (index + 1) * 10) for index, p in enumerate(PRESETS)]


def add_presets(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """开通租户时创建预置类型（加入当前事务）。"""
    session.add_all(TodoType(**row) for row in preset_rows(tenant_id))


async def ensure_presets(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """补齐缺少的预置类型（已有的不变）。预置类型不能删除，所以缺少的只会是新增的预置类型。"""
    existing = set(
        (await session.scalars(select(TodoType.code).where(TodoType.code.in_(PRESET_CODES)))).all()
    )
    missing = [row for row in preset_rows(tenant_id) if row["code"] not in existing]
    if missing:
        await session.execute(
            insert(TodoType)
            .values(missing)
            .on_conflict_do_nothing(index_elements=["tenant_id", "code"])
        )


async def type_by_code(session: AsyncSession, tenant_id: uuid.UUID, code: str) -> TodoType:
    """按编码取类型；预置类型缺少时先补齐。"""
    row = await session.scalar(select(TodoType).where(TodoType.code == code))
    if row is None:
        await ensure_presets(session, tenant_id)
        row = await session.scalar(select(TodoType).where(TodoType.code == code))
    if row is None:
        raise LookupError(f"todo type {code} is missing")
    return row
