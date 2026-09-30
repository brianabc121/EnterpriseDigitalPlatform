"""时限（设计文档 §24.6）：响应时限和完成时限按工作时间计算（沿用默认路由策略的工作时间），
客户明确提出的时间优先作为截止时间。截止前提醒一次、逾期时提醒一次、逾期超过设定时长升级给主管；
等待客户时暂停计时，恢复后顺延暂停的工作时长。
"""

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.routing.assign import PolicyResolver
from app.modules.routing.hours import (
    DEFAULT_TZ,
    add_business_minutes,
    business_day_minutes,
    business_minutes_between,
)
from app.modules.todos.models import Todo, TodoType


async def business_hours(session: AsyncSession) -> dict[str, Any] | None:
    """租户默认路由策略的工作时间（为空表示全天）。"""
    return (await PolicyResolver(session).default()).business_hours


def tz_of(spec: dict[str, Any] | None) -> ZoneInfo:
    return ZoneInfo((spec or {}).get("tz") or DEFAULT_TZ)


def resolve_minutes(type_: TodoType, spec: dict[str, Any] | None) -> int | None:
    """类型的完成时限（工作分钟）。按工作日设置时，一个工作日是工作时间里最长的一天。"""
    if type_.sla_resolve_days:
        return type_.sla_resolve_days * business_day_minutes(spec)
    return type_.sla_resolve_minutes


def suggested_due(
    type_: TodoType, spec: dict[str, Any] | None, start: datetime, expected_at: datetime | None
) -> datetime | None:
    if expected_at is not None:
        return expected_at
    minutes = resolve_minutes(type_, spec)
    return add_business_minutes(spec, start, minutes) if minutes else None


def start_clock(
    todo: Todo,
    type_: TodoType,
    spec: dict[str, Any] | None,
    start: datetime,
    *,
    due_at: datetime | None = None,
) -> None:
    """进入待办列表时开始计时：截止时间（指定的、客户期望的或按时限）、响应时限和提醒。"""
    todo.due_at = due_at or suggested_due(type_, spec, start, todo.expected_at)
    todo.respond_due_at = (
        add_business_minutes(spec, start, type_.sla_response_minutes)
        if type_.sla_response_minutes
        else None
    )
    todo.paused_at = None
    schedule_reminders(todo, type_, spec, start)


def schedule_reminders(
    todo: Todo, type_: TodoType, spec: dict[str, Any] | None, now: datetime
) -> None:
    """按截止时间安排到期提醒和升级（截止时间变化后重新安排）。"""
    todo.overdue_notified_at = None
    todo.escalated_at = None
    if todo.due_at is None:
        todo.remind_at = todo.escalate_at = None
        return
    remind_at = todo.due_at - timedelta(minutes=type_.remind_before_minutes)
    todo.remind_at = remind_at if type_.remind_before_minutes > 0 and remind_at > now else None
    todo.escalate_at = (
        add_business_minutes(spec, todo.due_at, type_.escalate_after_minutes)
        if type_.escalate_after_minutes > 0
        else None
    )


def resume_clock(todo: Todo, type_: TodoType, spec: dict[str, Any] | None, now: datetime) -> None:
    """等待客户结束：截止时间顺延暂停的工作时长。"""
    paused_at, todo.paused_at = todo.paused_at, None
    if paused_at is None or todo.due_at is None:
        return
    remaining = business_minutes_between(spec, paused_at, todo.due_at)
    if todo.due_at > paused_at:
        todo.due_at = add_business_minutes(spec, now, remaining)
    if todo.respond_due_at is not None and todo.first_response_at is None:
        left = business_minutes_between(spec, paused_at, todo.respond_due_at)
        if todo.respond_due_at > paused_at:
            todo.respond_due_at = add_business_minutes(spec, now, left)
    schedule_reminders(todo, type_, spec, now)


def pending_remind_at(spec: dict[str, Any] | None, created_at: datetime, minutes: int) -> datetime:
    return add_business_minutes(spec, created_at, minutes)
