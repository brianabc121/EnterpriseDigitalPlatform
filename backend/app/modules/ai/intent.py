"""AI 意图判断（设计文档 §32）：按客户的消息判断下单意向、真实意图、在意什么、情绪和要不要人工。

- 用什么判断：运营后台把"意图判断"路由到判断模型（TypeSafe Jev）或对话模型的轻量模型，没有路由时用
  环境变量配置的判断模型（见 llm_router.judge）。判断模型一次请求回答所有问题；轻量模型按同样的问题
  输出 JSON。
- 什么时候判断：客户的文字消息归入进行中的会话后登记（schedule，1 秒后，合并连续的消息），实时消费
  进程领取后判断（run_due）；AI 接待回复前确保有覆盖客户最新消息的结果（for_reply）。已经判断过最新
  一条客户消息的不再判断；失败 30 秒后重试，最多 3 次，之后等客户的下一条消息；每个会话最多 60 次。
- 结果存在 session_intents（每个会话一行，不存消息原文），推送给接待坐席和旁听、协助的同事；人工接待
  中客户第一次到"准备下单"时，坐席助手提醒一次。
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.integrations.llm import LLMUnavailable
from app.integrations.typesafe import Answer, Question
from app.modules.ai import copilot, gateway, pii, prompts
from app.modules.ai import service as ai_service
from app.modules.ai.llm_router import JudgeTarget
from app.modules.ai.models import AiSettings, SessionIntent
from app.modules.ai.prompts import Turn
from app.modules.ai.schemas import (
    IntentHistoryPoint,
    IntentJudgmentOut,
    IntentOption,
    SessionIntentOut,
)
from app.modules.billing.entitlements import has_feature
from app.modules.conversation import outbox
from app.modules.conversation.models import (
    ChatSession,
    Message,
    SenderType,
    SessionStatus,
    SessionWatcher,
)
from app.modules.routing.assign import PolicyResolver
from app.modules.routing.priority import intent_names
from app.observability import tracing
from app.observability.context import bind_tenant

logger = logging.getLogger(__name__)

SIGNAL = "intent.updated"

# ---- 问题与选项 ----

STAGES = ("没有购买意向", "随便了解", "有兴趣", "意向明确", "准备下单")
PURCHASE_LEVELS = [
    "没有购买意向：寒暄、闲聊、咨询售后或订单进度、投诉，或者和购买无关",
    "随便了解：泛泛地问公司或商品，没有具体想买的东西",
    "有兴趣：问具体商品的规格、用法、效果或价格，在比较和挑选",
    "意向明确：问库存、发货时间、优惠、付款方式，或者问怎么购买",
    "准备下单：明确说要买，给出数量、收货信息，或者要求下单、付款",
]
# 真实意图：编码 → （名称，给模型的说明）。租户自定义的编码是 "x:名称"。
INTENTS: dict[str, tuple[str, str]] = {
    "product": ("了解商品", "了解商品：功能、规格、材质、尺寸、用法、效果"),
    "price": ("询价比价", "问价格或优惠：多少钱、能不能便宜、有没有活动，或者拿别家的价格比较"),
    "purchase": ("购买下单", "要购买：怎么下单、付款方式、要多少、收货信息"),
    "delivery": ("库存发货", "库存和发货：有没有货、什么时候发货、多久能到、运费"),
    "order_status": ("查订单", "查已经下的订单：进度、物流、催发货"),
    "order_change": ("改订单", "改已经下的订单：改地址、改数量、取消订单"),
    "after_sales": ("售后", "售后：退货、换货、维修、质量问题、使用中出了问题"),
    "complaint": ("投诉", "投诉或强烈不满：投诉服务或商品、要求赔偿"),
    "invoice": ("发票手续", "发票、对公转账、合同等手续"),
    "cooperation": ("合作代理", "合作：批发、代理、加盟、商务合作"),
    "human": ("要人工", "要求人工客服、真人或负责人"),
    "other": ("闲聊其他", "寒暄、闲聊、打招呼，或者看不出具体诉求"),
}
CUSTOM_PREFIX = "x:"
CUSTOM_LIMIT = 10
CONCERNS: dict[str, tuple[str, str]] = {
    "price": ("价格", "价格：嫌贵、想要优惠、在比价"),
    "quality": ("质量效果", "质量和效果：好不好用、耐不耐用、是不是正品"),
    "delivery": ("发货时效", "时效：多久发货、多久能到、能不能赶上时间"),
    "service": ("售后保障", "售后保障：能不能退换、保修、安装"),
    "trust": ("是否可靠", "是否可靠：担心被骗、要看资质、评价或实物"),
    "none": ("没有明显顾虑", "没有明显的顾虑"),
}
EMOTIONS = ("平静", "有些着急", "生气激动")
EMOTION_LEVELS = ["平静或积极", "有些着急或不满", "生气、愤怒或情绪激动"]
OTHER_ROUTE = "其他"

# ---- 阈值（设计文档 §32.3、§32.6，按评测结果调整） ----

INTENT_THRESHOLD = 0.5  # 第 3、4 级的概率之和达到它视为有下单意向
HUMAN_THRESHOLD = 0.85  # 要人工：AI 接待立即转人工
READY_THRESHOLD = 0.6  # "准备下单"的提醒、高意向转人工
CONCERN_THRESHOLD = 0.35
ROUTE_THRESHOLD = 0.5
NEGATIVE_EMOTION = 1.5  # 情绪刻度达到它计入"负面情绪"软信号

# ---- 判断的范围与节奏 ----

TRANSCRIPT_MESSAGES = 12
MESSAGE_CHARS = 300
TRANSCRIPT_CHARS = 2000
LATEST_MESSAGES = 5
LOAD_MESSAGES = 40
MAX_JUDGMENTS = 60
MAX_FAILURES = 3
RETRY = timedelta(seconds=30)
LEASE = timedelta(minutes=1)
HISTORY_LIMIT = 20
CONCURRENCY = 8
# 单独发的寒暄、语气词不判断（"好的""可以"可能是在确认下单，照常判断）。
_TRIVIAL = frozenset(
    {
        "你好",
        "您好",
        "在吗",
        "在么",
        "在不在",
        "hi",
        "hello",
        "哈喽",
        "谢谢",
        "谢了",
        "多谢",
        "嗯",
        "嗯嗯",
        "哦",
        "噢",
        "喔",
        "哈哈",
    }
)
_ROLE_NAMES = {"customer": "客户", "agent": "客服", "bot": "智能客服"}
_ROLES: dict[str, str] = {
    SenderType.CUSTOMER: "customer",
    SenderType.BOT: "bot",
    SenderType.AGENT: "agent",
}
_PLACEHOLDER = {"image": "[图片]", "file": "[文件]", "voice": "[语音]", "video": "[视频]"}
_OPEN = (
    SessionStatus.AI_SERVING,
    SessionStatus.QUEUED,
    SessionStatus.HUMAN_SERVING,
    SessionStatus.TRANSFERRING,
)


def trivial(text: str) -> bool:
    core = text.strip().strip("。.!！~～?？,，、 ").lower()
    return not core or core in _TRIVIAL


def stage_label(stage: int | None) -> str | None:
    return STAGES[stage] if stage is not None and 0 <= stage < len(STAGES) else None


def intent_label(code: str | None) -> str | None:
    if not code:
        return None
    if code.startswith(CUSTOM_PREFIX):
        return code.removeprefix(CUSTOM_PREFIX)
    known = INTENTS.get(code)
    return known[0] if known else code


def concern_label(code: str) -> str:
    known = CONCERNS.get(code)
    return known[0] if known else code


def emotion_label(value: float | None) -> str | None:
    if value is None:
        return None
    return EMOTIONS[0] if value < 0.5 else EMOTIONS[1] if value < NEGATIVE_EMOTION else EMOTIONS[2]


def percent(value: float) -> str:
    return f"{round(value * 100)}%"


def intent_options(custom: list[Any] | None) -> dict[str, tuple[str, str]]:
    """真实意图的选项：内置的（"闲聊其他"放在最后）加上租户自定义的。"""
    options = {code: value for code, value in INTENTS.items() if code != "other"}
    for item in (custom or [])[:CUSTOM_LIMIT]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:16]
        if not name:
            continue
        description = str(item.get("description") or "").strip()[:60]
        options[CUSTOM_PREFIX + name] = (name, f"{name}：{description}" if description else name)
    options["other"] = INTENTS["other"]
    return options


def build_questions(
    options: dict[str, tuple[str, str]], routes: list[str]
) -> tuple[dict[str, Question], dict[str, str]]:
    """判断模型的问题，和"选项键 → 意图编码"（自定义意图在请求里用 x1、x2 这样的键）。"""
    criteria: dict[str, str] = {}
    back: dict[str, str] = {}
    custom = 0
    for code, (_, description) in options.items():
        key = code
        if code.startswith(CUSTOM_PREFIX):
            custom += 1
            key = f"x{custom}"
        criteria[key] = description
        back[key] = code
    questions = {
        "purchase": Question(
            "score",
            "根据客户自己说的话，判断客户现在想下单购买的程度。只看客户的表达，不看客服说了什么。",
            PURCHASE_LEVELS,
        ),
        "intent": Question(
            "choice", "客户这几句话真正想解决的是什么（看需求本身，不看寒暄和客气话）", criteria
        ),
        "concern": Question(
            "choice",
            "客户现在最在意、最担心的是什么",
            {code: description for code, (_, description) in CONCERNS.items()},
        ),
        "emotion": Question("score", "客户现在的情绪", EMOTION_LEVELS),
        "human": Question(
            "noul",
            "客户在要求转人工客服、找真人，或者要负责人来处理",
            {
                "true": "明确提出要人工、真人或负责人",
                "false": "没有提出这样的要求，只是在咨询或者表达情绪",
            },
        ),
    }
    if routes:
        route_criteria = {name: f"客户的诉求属于「{name}」" for name in routes}
        route_criteria[OTHER_ROUTE] = "都不符合"
        questions["route"] = Question(
            "choice", "客户的诉求属于哪一类（用于分配给对应的客服组）", route_criteria
        )
    return questions, back


# ---- 判断结果 ----


@dataclass
class Judgment:
    """一次判断：下单意向 5 级的概率（distribution）和其他问题的结果。"""

    distribution: list[float]
    score: float
    intent: str | None = None
    intent_probability: float | None = None
    intents: dict[str, float] = field(default_factory=dict)
    concern_probabilities: dict[str, float] = field(default_factory=dict)
    emotion: float | None = None
    human: float | None = None
    route: str | None = None
    source: str = "judge"
    model: str = ""

    @property
    def stage(self) -> int:
        return max(range(len(self.distribution)), key=lambda level: self.distribution[level])

    @property
    def stage_probability(self) -> float:
        return self.distribution[self.stage]

    @property
    def purchase_probability(self) -> float:
        return min(1.0, sum(self.distribution[3:]))

    @property
    def has_purchase_intent(self) -> bool:
        return self.purchase_probability >= INTENT_THRESHOLD

    @property
    def concerns(self) -> list[str]:
        found = [
            code
            for code, p in self.concern_probabilities.items()
            if code in CONCERNS and code != "none" and p >= CONCERN_THRESHOLD
        ]
        return sorted(found, key=lambda code: -self.concern_probabilities[code])[:2]

    def reached(self, stage: int) -> bool:
        """到了这个阶段或更高（概率之和达到阈值）。"""
        return sum(self.distribution[stage:]) >= READY_THRESHOLD

    def top_intents(self, limit: int = 3) -> list[tuple[str, float]]:
        ranked = sorted(self.intents.items(), key=lambda item: -item[1])
        return [(code, p) for code, p in ranked if p > 0][:limit]

    def signals(self) -> dict[str, Any]:
        """写进 AI 判定留痕的摘要。"""
        return {
            "stage": self.stage,
            "purchase": round(self.purchase_probability, 2),
            "intent": self.intent,
            "concerns": self.concerns,
            "emotion": None if self.emotion is None else round(self.emotion, 2),
            "human": None if self.human is None else round(self.human, 2),
            "source": self.source,
        }


def _distribution(answer: Answer, levels: int) -> list[float]:
    """刻度各级的概率；只有加权位置时按位置分到相邻的两级。"""
    probabilities = [answer.probabilities.get(str(level), 0.0) for level in range(levels)]
    total = sum(probabilities)
    if total > 0:
        return [p / total for p in probabilities]
    score = max(0.0, min(float(levels - 1), answer.score or 0.0))
    low = int(score)
    high = min(levels - 1, low + 1)
    result = [0.0] * levels
    result[high] += score - low
    result[low] += 1 - (score - low)
    return result


def interpret(
    answers: dict[str, Answer], back: dict[str, str], routes: list[str]
) -> Judgment | None:
    """判断模型的回答 → 判断结果；没有下单意向的回答时为空。"""
    purchase = answers.get("purchase")
    if purchase is None:
        return None
    distribution = _distribution(purchase, len(PURCHASE_LEVELS))
    score = (
        purchase.score
        if purchase.score is not None
        else sum(level * p for level, p in enumerate(distribution))
    )
    judgment = Judgment(distribution=distribution, score=round(score, 4))
    intent = answers.get("intent")
    if intent is not None and intent.choice in back:
        judgment.intent = back[intent.choice]
        judgment.intents = {
            back[key]: round(p, 4) for key, p in intent.probabilities.items() if key in back
        }
        judgment.intent_probability = judgment.intents.get(judgment.intent, intent.confidence)
    concern = answers.get("concern")
    if concern is not None:
        judgment.concern_probabilities = {
            key: round(p, 4) for key, p in concern.probabilities.items() if key in CONCERNS
        }
        if not judgment.concern_probabilities and concern.choice in CONCERNS:
            judgment.concern_probabilities = {concern.choice: concern.confidence or 1.0}
    emotion = answers.get("emotion")
    if emotion is not None and emotion.score is not None:
        judgment.emotion = round(emotion.score, 4)
    human = answers.get("human")
    if human is not None:
        judgment.human = human.probability
    route = answers.get("route")
    if route is not None and route.choice in routes:
        p = route.probabilities.get(route.choice, route.confidence or 0.0)
        if p >= ROUTE_THRESHOLD:
            judgment.route = route.choice
    return judgment


def _unit(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return max(0.0, min(1.0, float(value)))


def parse_llm(
    content: str, options: dict[str, tuple[str, str]], routes: list[str]
) -> Judgment | None:
    """轻量模型输出的 JSON → 判断结果（它给的把握不可靠，概率只是近似）。"""
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    level = data.get("purchase")
    if isinstance(level, bool) or not isinstance(level, int | float):
        return None
    stage = max(0, min(len(STAGES) - 1, round(level)))
    confidence = max(0.4, _unit(data.get("purchase_confidence"), 0.7))
    rest = (1 - confidence) / (len(STAGES) - 1)
    distribution = [confidence if index == stage else rest for index in range(len(STAGES))]
    judgment = Judgment(distribution=distribution, score=float(stage), source="llm", model="")
    intent = data.get("intent")
    if isinstance(intent, str) and intent in options:
        judgment.intent = intent
        judgment.intent_probability = _unit(data.get("intent_confidence"), 0.7)
        judgment.intents = {intent: judgment.intent_probability}
    concerns = data.get("concerns")
    if isinstance(concerns, list):
        judgment.concern_probabilities = {
            str(code): 0.7 for code in concerns[:2] if str(code) in CONCERNS
        }
    emotion = data.get("emotion")
    if not isinstance(emotion, bool) and isinstance(emotion, int | float):
        judgment.emotion = max(0.0, min(2.0, float(emotion)))
    if "human" in data:
        judgment.human = _unit(data.get("human"), 0.0)
    route = data.get("route")
    if isinstance(route, str) and route in routes:
        judgment.route = route
    return judgment


def from_row(row: SessionIntent) -> Judgment | None:
    """保存的判断 → 判断结果（AI 回复用已有的结果时）。"""
    if row.stage is None:
        return None
    answers = row.answers or {}
    distribution = [float(p) for p in answers.get("purchase") or []]
    if len(distribution) != len(STAGES):
        distribution = [0.0] * len(STAGES)
        distribution[row.stage] = 1.0
    return Judgment(
        distribution=distribution,
        score=row.score if row.score is not None else float(row.stage),
        intent=row.intent,
        intent_probability=row.intent_probability,
        intents={str(k): float(v) for k, v in (answers.get("intents") or {}).items()},
        concern_probabilities={
            str(k): float(v) for k, v in (answers.get("concerns") or {}).items()
        },
        emotion=row.emotion,
        human=row.human,
        route=row.route,
        source=row.source or "judge",
        model=row.model or "",
    )


def prompt_block(judgment: Judgment) -> str:
    """AI 回复的系统提示里的【客户意图判断】（设计文档 §32.6）：只有固定的标签和回复要求。"""
    stage = judgment.stage
    lines = [
        "【客户意图判断】（根据客户的话自动判断，仅供参考；和客户的话矛盾时以客户的话为准）",
        f"下单意向：{STAGES[stage]}（有下单意向的把握 {percent(judgment.purchase_probability)}）",
    ]
    if judgment.intent:
        lines.append(f"真实意图：{intent_label(judgment.intent)}")
    concerns = judgment.concerns
    if concerns:
        lines.append(f"客户在意：{'、'.join(concern_label(c) for c in concerns)}")
    if judgment.emotion is not None:
        lines.append(f"情绪：{emotion_label(judgment.emotion)}")
    rules: list[str] = []
    if stage >= 3:
        rules.append(
            "客户购买意向明确：先直接回答客户问的，再顺势推进下单（有下单工具时问清商品和数量，"
            "没有时说明可以由人工客服下单），不要硬推。"
        )
    elif stage <= 1:
        rules.append("客户还在随便了解：简洁回答，不要催客户下单。")
    if "price" in concerns:
        rules.append("客户在意价格：说明建议零售价和商品的价值，不要承诺优惠。")
    others = [concern_label(c) for c in concerns if c != "price"]
    if others:
        rules.append(f"客户在意{'、'.join(others)}：优先回答这一点。")
    if judgment.emotion is not None and judgment.emotion >= 1:
        rules.append("客户有些着急或不满：先回应客户的感受，再回答问题。")
    if rules:
        lines.append("回复要求：" + "".join(rules))
    return "\n".join(lines)


def judgment_out(judgment: Judgment) -> IntentJudgmentOut:
    stage = judgment.stage
    intent = judgment.intent
    return IntentJudgmentOut(
        stage=stage,
        stage_label=STAGES[stage],
        stage_probability=round(judgment.stage_probability, 4),
        purchase_probability=round(judgment.purchase_probability, 4),
        has_purchase_intent=judgment.has_purchase_intent,
        score=round(judgment.score, 4),
        distribution=[round(p, 4) for p in judgment.distribution],
        intent=intent,
        intent_label=intent_label(intent),
        intent_probability=judgment.intent_probability,
        intents=[
            IntentOption(code=code, label=intent_label(code) or code, probability=round(p, 4))
            for code, p in judgment.top_intents()
        ],
        concerns=[
            IntentOption(
                code=code,
                label=concern_label(code),
                probability=round(judgment.concern_probabilities.get(code, 0.0), 4),
            )
            for code in judgment.concerns
        ],
        emotion=judgment.emotion,
        emotion_label=emotion_label(judgment.emotion),
        human_probability=judgment.human,
        route=judgment.route,
        source="llm" if judgment.source == "llm" else "judge",
        model=judgment.model or None,
    )


def session_out(row: SessionIntent) -> SessionIntentOut | None:
    """会话最新的判断（还没有判断过时为空）。"""
    judgment = from_row(row)
    if judgment is None or row.judged_at is None:
        return None
    history: list[IntentHistoryPoint] = []
    for point in row.history or []:
        try:
            stage = int(point["stage"])
            at = datetime.fromisoformat(str(point["at"]))
        except (KeyError, TypeError, ValueError):
            continue
        history.append(
            IntentHistoryPoint(
                at=at,
                stage=stage,
                stage_label=stage_label(stage) or "",
                purchase_probability=float(point.get("p") or 0),
                intent_label=intent_label(point.get("intent")),
            )
        )
    base = judgment_out(judgment).model_dump()
    # 保存的阶段概率等以写入时为准（分布可能四舍五入过）。
    base.update(
        stage=row.stage,
        stage_label=stage_label(row.stage) or "",
        stage_probability=row.stage_probability or 0.0,
        purchase_probability=row.purchase_probability or 0.0,
        has_purchase_intent=(row.purchase_probability or 0.0) >= INTENT_THRESHOLD,
    )
    return SessionIntentOut(
        **base,
        session_id=row.session_id,
        message_id=row.message_id,
        judged_at=row.judged_at,
        peak_stage=row.peak_stage,
        peak_at=row.peak_at,
        pending=row.due_at is not None,
        history=history,
    )


# ---- 对话内容 ----


def message_text(message: Message) -> str:
    return message.text_plain or _PLACEHOLDER.get(message.content_type, "[消息]")


def split_turns(messages: list[Message]) -> tuple[list[Turn], list[Message]]:
    """（之前的对话，客户最新的消息）：最新的消息是最后一条客服或智能客服的消息之后，客户发的文字消息。"""
    relevant = [m for m in messages if m.sender_type in _ROLES]
    index = len(relevant)
    while index > 0 and relevant[index - 1].sender_type == SenderType.CUSTOMER:
        index -= 1
    latest = [m for m in relevant[index:] if (m.text_plain or "").strip()][-LATEST_MESSAGES:]
    history = [Turn(_ROLES[m.sender_type], message_text(m)) for m in relevant[:index]]
    return history, latest


def build_state(history: list[Turn], latest: list[str]) -> str:
    """给判断模型的对话：最近 12 条、每条最多 300 字、总共最多 2,000 字，最后单独列出
    客户最新的消息；手机号、身份证号等换成占位符。"""
    mapping: dict[str, str] = {}

    def clip(text: str) -> str:
        return pii.mask(" ".join(text.split())[:MESSAGE_CHARS], mapping)[0]

    latest_lines = [f"客户：{clip(text)}" for text in latest if text.strip()][-LATEST_MESSAGES:]
    budget = TRANSCRIPT_CHARS - sum(len(line) for line in latest_lines)
    lines: list[str] = []
    for turn in reversed(history[-TRANSCRIPT_MESSAGES:]):
        line = f"{_ROLE_NAMES.get(turn.role, '客服')}：{clip(turn.text)}"
        if len(line) > budget:
            break
        lines.append(line)
        budget -= len(line)
    lines.reverse()
    return "\n".join(
        [
            "以下是在线客服对话，最早的在前；个人信息已替换为 [手机号1] 这样的占位符。",
            *(lines or ["（之前没有对话）"]),
            "",
            "【客户最新的消息】",
            *latest_lines,
        ]
    )


# ---- 判断 ----


async def judge(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    target: JudgeTarget,
    state: str,
    *,
    custom: list[Any] | None,
    routes: list[str],
    session_id: uuid.UUID | None = None,
) -> Judgment:
    """判断一次；判断模型或轻量模型不可用、回答不完整时抛出 LLMUnavailable。"""
    options = intent_options(custom)
    if target.kind == "judge" and target.judge is not None:
        questions, back = build_questions(options, routes)
        result = await gateway.decide(
            ctx, tenant_id, target.judge, state, questions, session_id=session_id
        )
        judgment = interpret(result.answers, back, routes)
        if judgment is None:
            raise LLMUnavailable("判断模型没有给出下单意向")
        judgment.source, judgment.model = "judge", result.model
        return judgment
    messages = prompts.intent_messages(
        state=state,
        purchase=PURCHASE_LEVELS,
        intents={code: description for code, (_, description) in options.items()},
        concerns={code: description for code, (_, description) in CONCERNS.items()},
        emotions=EMOTION_LEVELS,
        routes=routes,
    )
    chat = await gateway.chat(
        ctx,
        tenant_id,
        messages,
        scene="intent",
        fast=True,
        json_mode=True,
        max_tokens=200,
        session_id=session_id,
    )
    judgment = parse_llm(chat.content, options, routes)
    if judgment is None:
        raise LLMUnavailable("轻量模型的意图判断格式不正确")
    judgment.source, judgment.model = "llm", chat.model
    return judgment


async def available(ctx: AppContext, tenant_id: uuid.UUID) -> JudgeTarget | None:
    return await ctx.llms.judge(tenant_id)


# ---- 保存与推送 ----


def apply(
    row: SessionIntent,
    judgment: Judgment,
    message_id: uuid.UUID | None,
    message_at: datetime | None,
    now: datetime,
) -> bool:
    """把判断写进会话的那一行；返回是不是第一次到"准备下单"（需要提醒坐席）。"""
    stage = judgment.stage
    row.message_id = message_id
    row.message_at = message_at
    row.judged_at = now
    row.stage = stage
    row.stage_probability = round(judgment.stage_probability, 4)
    row.purchase_probability = round(judgment.purchase_probability, 4)
    row.score = round(judgment.score, 4)
    row.intent = judgment.intent
    row.intent_probability = (
        None if judgment.intent_probability is None else round(judgment.intent_probability, 4)
    )
    row.concerns = judgment.concerns
    row.emotion = judgment.emotion
    row.human = judgment.human
    row.route = judgment.route
    row.source = judgment.source
    row.model = (judgment.model or "")[:128] or None
    row.answers = {
        "purchase": [round(p, 4) for p in judgment.distribution],
        "intents": dict(judgment.top_intents(5)),
        "concerns": judgment.concern_probabilities,
    }
    if row.peak_stage is None or stage > row.peak_stage:
        row.peak_stage = stage
        row.peak_at = now
    point = {
        "at": now.isoformat(),
        "message_id": str(message_id) if message_id else None,
        "stage": stage,
        "p": round(judgment.purchase_probability, 2),
        "score": round(judgment.score, 2),
        "intent": judgment.intent,
    }
    row.history = [*(row.history or []), point][-HISTORY_LIMIT:]
    row.judgments = (row.judgments or 0) + 1
    row.failures = 0
    return stage == len(STAGES) - 1 and judgment.reached(stage) and not row.ready_alerted


async def _recipients(session: AsyncSession, chat: ChatSession) -> set[uuid.UUID]:
    """推送对象：接待坐席和正在旁听、协助的同事。"""
    watchers = (
        await session.scalars(
            select(SessionWatcher.staff_id).where(
                SessionWatcher.session_id == chat.id, SessionWatcher.left_at.is_(None)
            )
        )
    ).all()
    found = {staff_id for staff_id in watchers if staff_id}
    if chat.assignee_id is not None:
        found.add(chat.assignee_id)
    return found


async def store(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    judgment: Judgment,
    message_id: uuid.UUID | None,
    message_at: datetime | None,
    *,
    lease: datetime | None = None,
) -> None:
    """保存判断、推送给坐席；并发判断时，覆盖的消息更早的结果不覆盖已有的。"""
    now = datetime.now(UTC)
    room: uuid.UUID | None = None
    async with ctx.db.tenant_session(tenant_id) as session:
        chat = await session.get(ChatSession, session_id)
        if chat is None:
            return
        await session.execute(
            insert(SessionIntent)
            .values(session_id=session_id, tenant_id=tenant_id)
            .on_conflict_do_nothing(index_elements=[SessionIntent.session_id])
        )
        row = await session.get(
            SessionIntent, session_id, with_for_update=True, populate_existing=True
        )
        assert row is not None
        stale = (
            row.message_at is not None and message_at is not None and message_at < row.message_at
        )
        if not stale:
            ready = apply(row, judgment, message_id, message_at, now)
            if chat.status in _OPEN:
                payload = {
                    "type": SIGNAL,
                    "session_id": str(chat.id),
                    "room_id": str(chat.room_id),
                    "stage": row.stage,
                    "purchase_probability": row.purchase_probability,
                    "intent": row.intent,
                    "real_intent": intent_label(row.intent),
                    "judged_at": now.isoformat(),
                }
                for staff_id in await _recipients(session, chat):
                    outbox.enqueue_signal(session, chat.room_id, staff_id, payload)
                    room = chat.room_id
            if ready and chat.status in copilot.SERVING and chat.assignee_id is not None:
                copilot.purchase_ready(
                    session, chat, message_id, percent(judgment.stage_probability)
                )
                row.ready_alerted = True
                room = chat.room_id
        if lease is not None and row.due_at == lease:
            row.due_at = None
        await session.commit()
    if room is not None:
        await outbox.flush_rooms(ctx, tenant_id, [room])


# ---- 登记与领取 ----


async def schedule(
    ctx: AppContext, session: AsyncSession, chat: ChatSession, message: Message, now: datetime
) -> bool:
    """客户的文字消息归入会话后（在归入的事务里）：值得判断时登记，稍后由实时消费进程判断。"""
    if message.sender_type != SenderType.CUSTOMER or chat.status not in _OPEN:
        return False
    text = (message.text_plain or "").strip()
    if not text or trivial(text):
        return False
    enabled = await session.scalar(
        select(AiSettings.intent_enabled).where(AiSettings.tenant_id == chat.tenant_id)
    )
    if enabled is False or await available(ctx, chat.tenant_id) is None:
        return False
    due = now + timedelta(seconds=ctx.settings.intent_debounce_seconds)
    statement = insert(SessionIntent).values(
        session_id=chat.id, tenant_id=chat.tenant_id, due_at=due
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[SessionIntent.session_id],
            set_={"due_at": statement.excluded.due_at, "failures": 0, "updated_at": func.now()},
        )
    )
    return True


async def claim(
    ctx: AppContext, *, limit: int, now: datetime
) -> list[tuple[uuid.UUID, uuid.UUID, datetime]]:
    """领取到期的判断（跨租户，平台连接），返回 (租户, 会话, 租约到期时间)。"""
    lease = now + LEASE
    async with ctx.db.platform_sessionmaker() as session:
        due = (
            select(SessionIntent.session_id)
            .where(SessionIntent.due_at <= now)
            .order_by(SessionIntent.due_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = await session.execute(
            update(SessionIntent)
            .where(SessionIntent.session_id.in_(due.scalar_subquery()))
            .values(due_at=lease)
            .returning(SessionIntent.tenant_id, SessionIntent.session_id)
        )
        claimed = [(tenant_id, session_id, lease) for tenant_id, session_id in rows]
        await session.commit()
    return claimed


async def run_due(ctx: AppContext, *, limit: int = 20, now: datetime | None = None) -> int:
    """判断所有到期的会话，返回领取的数量。"""
    claimed = await claim(ctx, limit=limit, now=now or datetime.now(UTC))
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(tenant_id: uuid.UUID, session_id: uuid.UUID, lease: datetime) -> None:
        async with semaphore:
            with (
                bind_tenant(tenant_id),
                tracing.tracer().start_as_current_span(
                    "intent judge", attributes={"edp.session_id": str(session_id)}
                ),
            ):
                try:
                    await process(ctx, tenant_id, session_id, lease)
                except Exception:
                    logger.exception("intent judgment failed for session %s", session_id)

    await asyncio.gather(*(one(*item) for item in claimed))
    return len(claimed)


async def _release(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    lease: datetime,
    *,
    retry_at: datetime | None = None,
    failed: bool = False,
) -> None:
    async with ctx.db.tenant_session(tenant_id) as session:
        row = await session.get(SessionIntent, session_id, with_for_update=True)
        if row is None:
            return
        if failed:
            row.failures = (row.failures or 0) + 1
            if row.failures >= MAX_FAILURES:
                retry_at = None
        if row.due_at == lease:
            row.due_at = retry_at
        await session.commit()


async def process(
    ctx: AppContext, tenant_id: uuid.UUID, session_id: uuid.UUID, lease: datetime
) -> None:
    """判断一个会话：读取（会话进行中、已启用、套餐包含 AI、有新的客户消息）→ 判断 → 保存。"""
    async with ctx.db.tenant_session(tenant_id) as session:
        row = await session.get(SessionIntent, session_id)
        chat = await session.get(ChatSession, session_id)
        if row is None or chat is None:
            return
        settings = await ai_service.load(session, tenant_id)
        skip = (
            chat.status not in _OPEN
            or not settings.intent_enabled
            or row.judgments >= MAX_JUDGMENTS
            or not await has_feature(session, tenant_id, "ai")
        )
        latest: list[Message] = []
        history: list[Turn] = []
        routes: list[str] = []
        if not skip:
            messages = (
                await session.scalars(
                    select(Message)
                    .where(Message.session_id == chat.id)
                    .order_by(Message.sent_at.desc(), Message.id.desc())
                    .limit(LOAD_MESSAGES)
                )
            ).all()
            history, latest = split_turns(list(reversed(messages)))
            policy = await PolicyResolver(session).for_channel(chat.channel_account_id)
            routes = intent_names(policy.intent_routes or [])
        custom = list(settings.custom_intents or [])
        judged = row.message_id
    if skip or not latest or latest[-1].id == judged:
        await _release(ctx, tenant_id, session_id, lease)
        return
    target = await available(ctx, tenant_id)
    if target is None:
        await _release(ctx, tenant_id, session_id, lease)
        return
    state = build_state(history, [message_text(m) for m in latest])
    try:
        judgment = await judge(
            ctx, tenant_id, target, state, custom=custom, routes=routes, session_id=session_id
        )
    except LLMUnavailable as exc:
        logger.warning("intent judgment unavailable for session %s: %s", session_id, exc)
        await _release(
            ctx, tenant_id, session_id, lease, retry_at=datetime.now(UTC) + RETRY, failed=True
        )
        return
    await store(
        ctx, tenant_id, session_id, judgment, latest[-1].id, latest[-1].sent_at, lease=lease
    )


async def for_reply(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    settings: AiSettings,
    *,
    history: list[Turn],
    latest: list[Message],
    routes: list[str],
) -> Judgment | None:
    """AI 回复前：有覆盖客户最新消息的判断就用它，没有就当场判断（失败时为空，不影响回复）。"""
    texts = [m for m in latest if (m.text_plain or "").strip()]
    if not (settings.intent_enabled and settings.intent_in_reply) or not texts:
        return None
    newest = texts[-1]
    async with ctx.db.tenant_session(tenant_id) as session:
        row = await session.get(SessionIntent, session_id)
        if row is not None and row.message_id == newest.id:
            return from_row(row)
    target = await available(ctx, tenant_id)
    if target is None:
        return None
    state = build_state(history, [message_text(m) for m in texts])
    try:
        judgment = await judge(
            ctx,
            tenant_id,
            target,
            state,
            custom=settings.custom_intents,
            routes=routes,
            session_id=session_id,
        )
    except LLMUnavailable as exc:
        logger.warning("intent judgment for the reply failed in session %s: %s", session_id, exc)
        return None
    await store(ctx, tenant_id, session_id, judgment, newest.id, newest.sent_at)
    return judgment
