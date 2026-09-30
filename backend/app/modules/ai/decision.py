"""转人工决策与回复护栏（设计文档 §11.1、§11.2）。纯函数，便于单独测试和调参。

- 硬触发：客户要求人工、敏感诉求、VIP 客户 → 立即转人工，不调用大模型。
- 后置护栏：回复为空、过长、出现资料里没有的承诺类话术、敏感词 → 不发送；连续两次转人工。
- 软信号加权：知识相关度低、模型把握低、负面情绪、重复提问、否定回答、轮数过多，得分达到阈值转人工。
"""

from dataclasses import dataclass, field

from app.modules.kb.text import similarity

HUMAN_REQUEST = (
    "转人工",
    "人工客服",
    "人工服务",
    "找人工",
    "要人工",
    "接人工",
    "真人",
    "找客服",
    "联系客服",
)
SENSITIVE = (
    "投诉",
    "退款",
    "赔偿",
    "赔付",
    "索赔",
    "律师",
    "起诉",
    "法院",
    "报警",
    "12315",
    "消协",
    "曝光",
    "删除我的信息",
    "删除个人信息",
    "注销账号",
)
NEGATIVE = (
    "生气",
    "愤怒",
    "垃圾",
    "骗子",
    "骗人",
    "太差",
    "差评",
    "失望",
    "不满意",
    "气死",
    "无语",
    "坑人",
    "什么破",
)
NEGATION = (
    "没用",
    "不是这个意思",
    "答非所问",
    "没解决",
    "不对",
    "看不懂",
    "还是不行",
    "不是我问的",
)
PROMISES = ("保证", "一定会", "肯定能", "承诺", "赔偿", "赔付", "全额退款", "包赔", "绝对")

WEIGHTS = {
    "low_relevance": 0.4,
    "low_confidence": 0.3,
    "negative": 0.3,
    "repeated": 0.3,
    "negation": 0.2,
    "too_many_turns": 0.2,
}
LOW_CONFIDENCE = 0.5
REPEAT_SIMILARITY = 0.6
REPEATS_TO_SIGNAL = 2
MAX_REPLY_CHARS = 500
GUARD_FAILURES_TO_HANDOFF = 2


def _contains(text: str, words: tuple[str, ...] | list[str]) -> str | None:
    return next((w for w in words if w and w in text), None)


def is_negative(text: str) -> bool:
    """客户的话里有负面情绪（生气、失望、连续感叹号等）。"""
    return bool(_contains(text, NEGATIVE)) or "！！" in text or "!!" in text


def promise_word(text: str) -> str | None:
    """回复里的承诺类用语。"""
    return _contains(text, PROMISES)


def hard_trigger(
    question: str,
    *,
    extra_handoff: list[str],
    extra_sensitive: list[str],
    customer_tags: list[str],
) -> str | None:
    """需要立即转人工的原因：customer_request、sensitive、vip；否则为空。"""
    if _contains(question, [*HUMAN_REQUEST, *extra_handoff]):
        return "customer_request"
    if _contains(question, [*SENSITIVE, *extra_sensitive]):
        return "sensitive"
    if any(tag.strip().lower() == "vip" for tag in customer_tags):
        return "vip"
    return None


def guard(reply: str, *, references: str, sensitive: list[str]) -> str | None:
    """回复不能发送的原因：empty、too_long、promise（资料里没有的承诺类话术）、sensitive；否则为空。"""
    text = reply.strip()
    if not text:
        return "empty"
    if len(text) > MAX_REPLY_CHARS:
        return "too_long"
    promise = _contains(text, PROMISES)
    if promise and promise not in references:
        return "promise"
    if _contains(text, sensitive):
        return "sensitive"
    return None


@dataclass
class Signals:
    low_relevance: bool = False
    low_confidence: bool = False
    negative: bool = False
    repeated: bool = False
    negation: bool = False
    too_many_turns: bool = False
    details: dict[str, float | int | str] = field(default_factory=dict)

    @property
    def score(self) -> float:
        return round(sum(w for name, w in WEIGHTS.items() if getattr(self, name)), 2)

    def active(self) -> dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in WEIGHTS}


def signals(
    question: str,
    *,
    best_relevance: float,
    relevance_threshold: float,
    confidence: float,
    previous_question: str | None,
    repeats: int,
    turns: int,
    max_turns: int,
) -> tuple[Signals, int]:
    """计算软信号；返回信号和更新后的重复提问次数。"""
    repeated_now = bool(previous_question) and (
        similarity(question, previous_question or "") >= REPEAT_SIMILARITY
    )
    repeats = repeats + 1 if repeated_now else 0
    result = Signals(
        low_relevance=best_relevance < relevance_threshold,
        low_confidence=confidence < LOW_CONFIDENCE,
        negative=is_negative(question),
        repeated=repeats >= REPEATS_TO_SIGNAL,
        negation=bool(_contains(question, NEGATION)),
        too_many_turns=turns >= max_turns,
        details={
            "best_relevance": round(best_relevance, 4),
            "confidence": round(confidence, 4),
            "repeats": repeats,
            "turns": turns,
        },
    )
    return result, repeats
