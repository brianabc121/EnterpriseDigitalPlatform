"""排队优先级与按意图分配（设计文档 §11.3）。纯函数，便于单独测试。

- 优先级 = 档位 × 10 + 退回队列的次数（最多 9）：VIP 客户（带策略里的优先标签）2 档；投诉、退款等
  敏感诉求或情绪激动 1 档（策略可以关闭）；其余 0 档。同一优先级按进入排队的时间排序。
- 意图：AI 识别出的意图与策略里的意图名称一致时用它；否则按客户说的话里出现的关键词匹配，
  取第一条命中的规则，分配到它指定的技能组。
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.modules.ai.decision import NEGATIVE, SENSITIVE

TIER = 10
VIP_TIER = 2
URGENT_TIER = 1
URGENT_REASONS = frozenset({"sensitive"})


def base_priority(
    *,
    priority_tags: Iterable[str],
    urgent_first: bool,
    customer_tags: Iterable[str],
    text: str,
    reason: str | None = None,
) -> int:
    if set(priority_tags) & set(customer_tags):
        return VIP_TIER * TIER
    if urgent_first and (
        reason in URGENT_REASONS or any(word in text for word in (*SENSITIVE, *NEGATIVE))
    ):
        return URGENT_TIER * TIER
    return 0


def bump(priority: int) -> int:
    """退回队列：同一档内往前排一位（每档最多 9 次）。"""
    return priority + 1 if priority % TIER < TIER - 1 else priority


def tier_of(priority: int) -> str:
    return {VIP_TIER: "vip", URGENT_TIER: "urgent"}.get(priority // TIER, "normal")


@dataclass(frozen=True)
class IntentMatch:
    intent: str
    skill_group_id: uuid.UUID
    by: str  # ai 或 keyword


def _routes(routes: list[Any]) -> list[tuple[str, list[str], uuid.UUID]]:
    parsed = []
    for route in routes:
        if not isinstance(route, dict):
            continue
        try:
            group = uuid.UUID(str(route["skill_group_id"]))
        except (KeyError, ValueError):
            continue
        keywords = [str(k) for k in route.get("keywords") or [] if k]
        parsed.append((str(route.get("intent") or ""), keywords, group))
    return [r for r in parsed if r[0]]


def intent_names(routes: list[Any]) -> list[str]:
    return [name for name, _, _ in _routes(routes)]


def match_intent(
    routes: list[Any], *, text: str, ai_intent: str | None = None
) -> IntentMatch | None:
    parsed = _routes(routes)
    if ai_intent:
        for name, _, group in parsed:
            if name == ai_intent:
                return IntentMatch(name, group, "ai")
    for name, keywords, group in parsed:
        if any(keyword in text for keyword in keywords):
            return IntentMatch(name, group, "keyword")
    return None
