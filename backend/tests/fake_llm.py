"""模拟的 OpenAI 兼容大模型服务：单元测试里作为 httpx 传输层，浏览器验收时作为独立服务运行。

- /v1/embeddings：按词项（中文二元组）哈希到 1024 维再归一化，意思相近的问题向量相近。
- /v1/chat/completions：按系统提示第一行的任务名作答：
  - 在线客服回复：取【参考资料】第一条的答案作为回复（有资料时把握 0.9），没有资料时用【商品信息】
    的第一条；待办、订单工具返回了答复话术、缺少的信息、要复述的订单、查到的商品或进度时，照着答复；
    商品库里没有找到时如实告诉客户（不转人工）；
    没有资料时回复无法回答并请求转人工。请求带了工具且 tool_plan 里有安排时，先返回工具调用，
    拿到工具结果后再回复（工具查到的资料也可以作为答案）。
  - 问题改写：按问号、分号、换行拆开，去掉寒暄；有上文且问题很短时补上上一句客户消息。
  - 转人工摘要：概括最后几句客户消息。
  - 坐席建议回复：把【商品信息】和参考资料的答案作为建议。
  - 会话小结：客户说过的话作为诉求，带"退"字时标签为售后。
  - 知识提炼：客户的问题与紧跟的客服回答组成问答；客服没能解答的问题记为缺口。
  - 优秀话术：坐席说的较长的话。
  - 待办解析：按关键词识别开票、回电、退换货、投诉、上门、报价、寄资料（只用系统提示里列出的
    类型），开票的抬头和税号从原话里取；坐席说"给您回电"时记为坐席答应的事；
    客户原话带"【低置信】"时置信度为 0.4。
  - 订单解析：客户说的"<商品> N 个/件/台"或"要 N 个 <商品>"是商品行，"收货人""电话""地址"
    后面的内容是收货信息，"货到付款""定金""月结"等是付款方式，"备注"后面的内容是备注。
  - 公司助理：带工具时按员工的话选工具（"待办"→ list_my_tasks，"订单"→ lookup_orders，
    "客户"→ lookup_customer，"记一下 / 提醒我"→ create_task，"完成"→ complete_task，
    "大家 / 团队"→ team_overview，"商品 / 库存"→ search_products，其他 → search_knowledge），
    拿到工具结果后把结果复述给员工；tool_plan 里有安排时优先按安排调用。
  - 群聊知识提炼：带问号的一句话后面紧跟别人的回答就是一个问答，没有人回答的记为缺口。
  - 巡检简报：复述待处理的问题数和最要紧的第一个问题。
  - 知识与制度核对：知识答案里的"数字 + 单位"（天、小时、元、%……）在制度里同一单位是别的数字时
    判为冲突，按制度的数字改写答案；一致时 consistent。
  - 制度转问答："标题：内容"或者带数字规定的句子写成问答（最多 2 条）。
  - 整理意向客户：客户第一句带"要、想、买、多少钱、价格、规格、有货"的话是想要什么；"贵、优惠、便宜"
    是价格，"考虑、商量"是还要考虑，"比较、别家"是在比较，"交期、多久"是交期；客户说"N 天后""下周"
    "明天"时按这个天数跟进，否则 3 天。
  - 意向客户跟进话术：问候客户，提到想要什么；有参考资料时引用第一条的第一句。
- /v1/rerank：问题词项被文档覆盖的比例作为相关度。
- /v1/systemone：模拟 TypeSafe 的判断模型（Jev，设计文档 §32）。按"客户最新的消息"里的关键词回答：
  下单意向（"我要""下单""地址是" → 准备下单；"有货""发货""优惠""怎么买" → 意向明确；"多少钱""规格"
  → 有兴趣；"看看""了解" → 随便了解；投诉、售后、查物流 → 没有）、真实意图（自定义意图按名称匹配）、
  在意什么、情绪、要人工（"人工""真人""负责人"）、分配意图（按意图名称和售前、售后、投诉的常用词）。
  对话模型收到"意图判断"任务时按同样的规则输出 JSON。judge_mode 为 down 时判断模型返回 503。
- /v1/audio/transcriptions：语音转文字。音频内容里带 "text=..." 时返回这段文字（测试和验收
  发的"语音"里写好要转写的内容），否则返回固定的文字。
- 可以切换模式模拟故障：down（503）、bad_json（不是 JSON）、promise（回复里带承诺类话术）、
  handoff（模型要求转人工）。独立运行时用 POST /_control {"mode": "down"} 切换；
  {"tool_plan": [["save_lead_info", {...}]]} 安排接下来的工具调用；{"judge_mode": "down"} 让判断模型
  返回 503。

独立运行：uv run python -m tests.fake_llm --port 8900
"""

import argparse
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx

from app.modules.ai.decision import NEGATIVE
from app.modules.ai.prompts import (
    NO_REFERENCE,
    TASK_ASSISTANT,
    TASK_CONTRACT,
    TASK_EXTRACT,
    TASK_GROUP_EXTRACT,
    TASK_INTENT,
    TASK_KB_ALIGN,
    TASK_KB_GAP,
    TASK_ORDER_EXTRACT,
    TASK_PHRASE,
    TASK_PROSPECT,
    TASK_PROSPECT_MESSAGE,
    TASK_REPLY,
    TASK_REWRITE,
    TASK_SESSION_SUMMARY,
    TASK_SUGGEST,
    TASK_SUMMARY,
    TASK_TODO_EXTRACT,
    TASK_WAKE_BRIEF,
)
from app.modules.kb.text import terms

DIM = 1024
MODEL = "fake-chat"
EMBED_MODEL = "fake-embed"


def embed_text(text: str) -> list[float]:
    vector = [0.0] * DIM
    for term in terms(text) or [text.strip().lower()]:
        digest = hashlib.sha256(term.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % DIM
        vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def _answers(system: str) -> list[str]:
    """从系统提示的【参考资料】里取出每条资料的答案或正文。"""
    if "【参考资料】" not in system:
        return []
    # 资料块在系统提示的最后（规则里也提到了"【参考资料】"）。
    block = system.rsplit("【参考资料】", 1)[1].strip()
    if block.startswith(NO_REFERENCE):
        return []
    entries = re.split(r"(?m)^\[\d+\] ", block)
    answers = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        if entry.startswith("问："):
            answers.append(entry.split("\n答：", 1)[-1].strip())
        else:
            answers.append(entry.split("\n", 1)[-1].strip())
    return answers


def _products(system: str) -> list[str]:
    """系统提示里【商品信息】的每一行（商品库里查到的商品）。"""
    match = re.search(r"【商品信息】\n(.*?)(?:\n\n|\Z)", system, re.S)
    if not match:
        return []
    return [line.removeprefix("- ").strip() for line in match.group(1).splitlines() if line]


# 坐席没能当场解答时常说的话：客户的问题记为"没有得到解答"。
_UNSURE = ("不确定", "不清楚", "稍后回复", "帮您问一下", "无法回答", "暂时无法", "确认一下")
_HANDOFF = ("转人工", "人工客服", "找客服")


def _extract(transcript: str) -> str:
    """知识提炼：客户的一句话后面紧跟客服的回答，就是一个问答；客服没能解答的记为缺口。"""
    lines = [
        (int(number), role, text.strip())
        for number, role, text in re.findall(
            r"(?ms)^\[(\d+)\] ([^：\n]+)：(.*?)(?=^\[\d+\] |\Z)", transcript
        )
    ]
    pairs: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for index, (number, role, text) in enumerate(lines):
        if role != "客户" or any(word in text for word in _HANDOFF):
            continue
        question = text.splitlines()[0]
        following = lines[index + 1] if index + 1 < len(lines) else None
        if following is None or following[1] == "客户":
            unresolved.append(question)
            continue
        answer = following[2]
        if any(word in answer for word in _UNSURE):
            unresolved.append(question)
            continue
        pairs.append(
            {
                "question": question,
                "answer": answer,
                "category": "",
                "generalizable": True,
                "time_sensitive": False,
                "confidence": 0.8,
                "evidence": [number, following[0]],
            }
        )
    return json.dumps({"qa_pairs": pairs, "unresolved_questions": unresolved}, ensure_ascii=False)


_GREETINGS = re.compile(r"^(你好|您好|在吗|请问|哈喽|hi|hello)[，,！!。\s]*", re.IGNORECASE)
_REFERENCE_WORDS = ("那", "它", "这个", "那个")


def _rewrite(content: str) -> str:
    """问题改写：拆开多个问题、去掉寒暄；有上文且问题很短时补上上一句客户消息。"""
    history, _, latest = content.partition("客户最新的消息：\n")
    previous = [
        line.removeprefix("客户：") for line in history.splitlines() if line.startswith("客户：")
    ]
    parts = []
    for part in re.split(r"[？?；;\n]", latest):
        part = _GREETINGS.sub("", part.strip()).strip("，,。 ")
        if not part:
            continue
        if previous and (len(part) < 6 or part.startswith(_REFERENCE_WORDS)):
            part = f"{previous[-1].rstrip('？?')} {part.lstrip('那它这个')}"
        parts.append(part)
    return json.dumps({"queries": parts[:3] or [latest.strip()]}, ensure_ascii=False)


def _session_summary(transcript: str) -> str:
    said = [
        line.split("：", 1)[1]
        for line in transcript.splitlines()
        if line.startswith("客户：") and "：" in line
    ]
    text = "；".join(said)[:100]
    tags = ["售后"] if "退" in text else ["咨询"]
    return json.dumps({"summary": f"客户咨询：{text}。", "tags": tags}, ensure_ascii=False)


def _phrases(transcript: str) -> str:
    lines = re.findall(r"(?m)^\[\d+\] 坐席：(.+)$", transcript)
    phrases = [{"title": line[:8], "content": line} for line in lines if len(line) >= 10][:3]
    return json.dumps({"phrases": phrases}, ensure_ascii=False)


# 待办解析的关键词：类型编码 → 客户原话里的词。
_TODO_WORDS: dict[str, tuple[str, ...]] = {
    "invoice": ("发票", "开票", "专票"),
    "callback": ("回电", "回个电话", "打电话", "给我电话"),
    "after_sales": ("退货", "换货", "换一个", "破损", "坏了", "维修"),
    "complaint": ("投诉", "赔偿"),
    "visit": ("上门", "安装"),
    "quote": ("报价", "批量", "什么价"),
    "send_materials": ("资料", "手册"),
}
_AGENT_PROMISE = ("给您回电", "回您电话", "给您打电话")


def _todo_extract(system: str, transcript: str) -> str:
    """待办解析：按关键词找客户的诉求和坐席答应的事。"""
    codes = set(re.findall(r"(?m)^- ([a-z][a-z0-9_]*)（", system))
    now_match = re.search(r"现在是 (\S+)。", system)
    lines = [
        (int(number), role, text.strip())
        for number, role, text in re.findall(
            r"(?ms)^\[(\d+)\] ([^：\n]+)：(.*?)(?=^\[\d+\] |\Z)", transcript
        )
    ]
    found: dict[str, dict[str, Any]] = {}
    for number, role, text in lines:
        matched: list[tuple[str, bool]] = []
        if role == "客户":
            matched = [
                (code, False)
                for code, words in _TODO_WORDS.items()
                if any(w in text for w in words)
            ]
        elif any(word in text for word in _AGENT_PROMISE):
            matched = [("callback", True)]
        for code, promised in matched:
            if code not in codes:
                continue
            item = found.setdefault(
                code,
                {
                    "type": code,
                    "title": text[:20],
                    "detail": "",
                    "fields": {},
                    "promised_by_agent": promised,
                    "due_hint": "",
                    "due_at": "",
                    "evidence": [],
                    "confidence": 0.4 if "【低置信】" in text else 0.8,
                },
            )
            item["detail"] = "；".join(filter(None, [item["detail"], text]))[:300]
            item["evidence"].append(number)
            if code == "invoice":
                kind = "增值税专用发票" if ("专票" in text or "专用" in text) else "普通发票"
                item["fields"].setdefault("invoice_type", kind)
                title = re.search(r"抬头[是为：:\s]*([^\s，,。；;]+)", text)
                tax = re.search(r"税号[是为：:\s]*([0-9A-Za-z]+)", text)
                if title:
                    item["fields"]["invoice_title"] = title.group(1)
                if tax:
                    item["fields"]["tax_no"] = tax.group(1)
            if "明天" in text and now_match:
                day = now_match.group(1)[:10]
                item["due_hint"] = "明天"
                item["due_at"] = f"{day}T18:00:00+08:00"
    for item in found.values():
        if item["due_at"]:
            # "明天"：现在的日期加一天。
            day = date.fromisoformat(item["due_at"][:10]) + timedelta(days=1)
            item["due_at"] = f"{day.isoformat()}T18:00:00+08:00"
    return json.dumps({"todos": list(found.values())}, ensure_ascii=False)


_QUANTITY = r"(\d+|[一两二三四五六七八九十])\s*(?:个|件|台|套|把|盒|箱|只|部)"
_NUMBERS = dict(zip("一两二三四五六七八九", (1, 2, 2, 3, 4, 5, 6, 7, 8, 9), strict=True))
_VERBS = re.compile(r"^(我想要|我想买|我要|想要|想买|再来|来|要|买|订)")
_PAYMENTS = (
    ("货到付款", "cod"),
    ("定金", "deposit"),
    ("月结", "credit"),
    ("先欠", "credit"),
    ("在线付", "online"),
    ("转账", "online"),
    ("微信支付", "online"),
)


def _order_extract(transcript: str) -> str:
    """订单解析：从客户的话里找商品行、收货信息、付款方式和备注。"""
    said = [
        text.strip()
        for role, text in re.findall(r"(?m)^\[\d+\] ([^：\n]+)：(.*)$", transcript)
        if role == "客户"
    ]
    items: list[dict[str, Any]] = []
    receiver = {"name": "", "phone": "", "address": ""}
    payment = note = ""
    for text in said:
        for clause in re.split(r"[，,。；;！!\n]", text):
            clause = clause.strip()
            match = re.search(_QUANTITY, clause)
            if match and not re.search(r"收货人|电话|地址", clause):
                before = _VERBS.sub("", clause[: match.start()].strip()).strip()
                after = clause[match.end() :].strip()
                product = (before or after).strip(" 的")
                raw = match.group(1)
                quantity = int(raw) if raw.isdigit() else _NUMBERS.get(raw, 1)
                if product:
                    items.append({"product": product, "quantity": quantity})
        name = re.search(r"(?:收货人|收件人|联系人)[是为：:\s]*([^\s，,。；;]+)", text)
        phone = re.search(r"\[手机号\d+\]|1\d{10}", text)
        address = re.search(r"(?:地址|寄到|送到)[是为：:\s]*([^，,。；;]+)", text)
        if name:
            receiver["name"] = name.group(1)
        if phone:
            receiver["phone"] = phone.group(0)
        if address:
            receiver["address"] = address.group(1).strip()
        payment = next((code for word, code in _PAYMENTS if word in text), payment)
        remark = re.search(r"备注[是为：:\s]*(.+)$", text)
        if remark:
            note = remark.group(1).strip()
    return json.dumps(
        {"items": items, "receiver": receiver, "payment": payment, "note": note},
        ensure_ascii=False,
    )


_ASSISTANT_TOOLS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("记一下", "提醒我", "记一件"), "create_task"),
    (("完成了", "做完了", "完成 "), "complete_task"),
    (("大家", "团队", "全员"), "team_overview"),
    (("客户待办", "待确认", "待认领"), "list_work_todos"),
    (("待办", "我有什么事", "今天要做"), "list_my_tasks"),
    (("订单",), "lookup_orders"),
    (("客户",), "lookup_customer"),
    (("商品", "库存", "价格"), "search_products"),
)


def _assistant_call(question: str, offered: set[str]) -> tuple[str, dict[str, Any]] | None:
    """按员工的话选一个工具；没有合适的用知识库检索。"""
    for words, name in _ASSISTANT_TOOLS:
        if name in offered and any(w in question for w in words):
            if name == "create_task":
                title = question
                for word in ("记一下", "提醒我", "记一件事", "：", ":"):
                    title = title.split(word, 1)[-1]
                return name, {"title": title.strip(" ，,。") or question[:20]}
            if name == "complete_task":
                key = question
                for word in ("完成了", "做完了", "完成 "):
                    key = key.split(word, 1)[-1]
                return name, {"key": key.strip(" ，,。")}
            if name == "list_my_tasks":
                which = "today" if "今天" in question else "all"
                return name, {"filter": "overdue" if "逾期" in question else which}
            if name == "list_work_todos":
                view = "pool" if "待认领" in question else "mine"
                return name, {"view": "pending" if "待确认" in question else view}
            if name == "lookup_orders":
                match = re.search(r"[A-Z]{2}\d{8}-\d{4}", question)
                q = match.group(0) if match else question.replace("订单", "").strip()
                return name, {"q": q}
            if name == "lookup_customer":
                return name, {"q": question.replace("客户", "").strip(" 的情况怎么样？?")}
            if name == "search_products":
                query = question.replace("商品", "").replace("库存", "")
                return name, {"query": query.strip(" 的有多少？?")}
            return name, {}
    if "search_knowledge" in offered:
        return "search_knowledge", {"query": question}
    return None


def _assistant_reply(messages: list[dict[str, Any]], question: str) -> str:
    outputs = [str(m.get("content") or "") for m in messages if m.get("role") == "tool"]
    if outputs:
        return outputs[-1][:500]
    return "我可以帮你查询待办、订单、客户和知识库，或者帮你记一件事。"


def _group_extract(transcript: str) -> str:
    """群聊知识提炼：带问号的一句话后面紧跟别人的回答就是一个问答，没有人回答的记为缺口。"""
    lines = [
        (int(number), role, text.strip())
        for number, role, text in re.findall(
            r"(?ms)^\[(\d+)\] ([^：\n]+)：(.*?)(?=^\[\d+\] |\Z)", transcript
        )
    ]
    pairs: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for index, (number, role, text) in enumerate(lines):
        question = text.splitlines()[0]
        if not question.rstrip().endswith(("？", "?")):
            continue
        following = lines[index + 1] if index + 1 < len(lines) else None
        if following is None or following[1] == role or following[2].rstrip().endswith(("？", "?")):
            unresolved.append(question)
            continue
        pairs.append(
            {
                "question": question,
                "answer": following[2],
                "category": "",
                "generalizable": True,
                "time_sensitive": False,
                "confidence": 0.8,
                "evidence": [number, following[0]],
            }
        )
    return json.dumps({"qa_pairs": pairs, "unresolved_questions": unresolved}, ensure_ascii=False)


# 意图判断（设计文档 §32）：按客户最新的消息里的关键词判断。
_NO_PURCHASE = (
    "投诉",
    "退货",
    "退款",
    "换货",
    "维修",
    "坏了",
    "物流",
    "快递到哪",
    "到哪了",
    "订单号",
)
_PURCHASE_WORDS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        4,
        (
            "我要",
            "要了",
            "下单",
            "买了",
            "拍了",
            "付款",
            "地址是",
            "收货人",
            "就这个",
            "给我来",
            "确认购买",
        ),
    ),
    (3, ("怎么买", "有货", "库存", "现货", "发货", "几天能到", "多久能到", "优惠", "包邮")),
    (2, ("多少钱", "价格", "价钱", "规格", "尺寸", "颜色", "材质", "效果", "型号", "区别", "哪款")),
    (1, ("看看", "了解", "介绍", "有什么", "你们家")),
)
_HUMAN_WORDS = ("人工", "真人", "负责人", "找客服")
_INTENT_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("human", _HUMAN_WORDS),
    ("complaint", ("投诉", "赔偿", "差评", "骗子")),
    ("after_sales", ("退货", "换货", "维修", "坏了", "质量问题", "退款")),
    ("order_change", ("改地址", "取消订单", "改数量")),
    ("order_status", ("物流", "快递到哪", "到哪了", "订单号", "催发货")),
    ("invoice", ("发票", "开票", "对公")),
    ("cooperation", ("批发", "代理", "加盟", "合作")),
    ("purchase", _PURCHASE_WORDS[0][1]),
    ("delivery", ("有货", "库存", "现货", "发货", "几天能到", "多久能到", "运费", "包邮")),
    ("price", ("多少钱", "价格", "价钱", "便宜", "优惠", "贵")),
    ("product", ("规格", "尺寸", "颜色", "材质", "效果", "型号", "区别", "怎么用", "质量")),
)
_CONCERN_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("price", ("贵", "便宜", "优惠", "价格", "多少钱", "折扣")),
    ("quality", ("质量", "耐用", "正品", "好不好用", "效果")),
    ("delivery", ("发货", "几天能到", "多久能到", "赶时间", "急用", "加急", "今天能发")),
    ("service", ("退换", "保修", "售后", "安装")),
    ("trust", ("靠谱", "骗", "正规", "资质", "真的假的")),
)
_MILD = ("着急", "急", "怎么还", "快点", "等了")
_ROUTE_WORDS: dict[str, tuple[str, ...]] = {
    "售后": ("退货", "换货", "维修", "坏了", "退款", "售后"),
    "投诉": ("投诉", "赔偿", "差评"),
    "售前": ("多少钱", "价格", "有货", "怎么买", "下单", "我要", "规格"),
    "技术": ("怎么用", "安装", "设置", "报错"),
}


def _latest_customer(state: str) -> str:
    """意图判断的对话里"客户最新的消息"。"""
    block = state.rsplit("【客户最新的消息】", 1)[-1]
    return "\n".join(
        line.removeprefix("客户：") for line in block.splitlines() if line.startswith("客户：")
    )


def _purchase_level(text: str) -> int:
    if any(word in text for word in _NO_PURCHASE):
        return 0
    return next((level for level, words in _PURCHASE_WORDS if any(w in text for w in words)), 0)


def _pick_intent(text: str, customs: dict[str, str], available: set[str]) -> str:
    """真实意图：要人工、投诉先看；租户自定义的按名称匹配；都没有时是 other。"""
    for code, words in _INTENT_WORDS[:2]:
        if code in available and any(w in text for w in words):
            return code
    for code, name in customs.items():
        if name and name in text:
            return code
    for code, words in _INTENT_WORDS[2:]:
        if code in available and any(w in text for w in words):
            return code
    return "other"


def _concerns(text: str) -> list[str]:
    return [code for code, words in _CONCERN_WORDS if any(w in text for w in words)][:2]


def _emotion(text: str) -> int:
    if any(word in text for word in NEGATIVE) or "！！" in text or "!!" in text:
        return 2
    return 1 if any(word in text for word in _MILD) else 0


def _route(text: str, names: list[str]) -> str:
    for name in names:
        if name in text or any(w in text for w in _ROUTE_WORDS.get(name, ())):
            return name
    return "其他"


def _spread(chosen: str, keys: list[str], weight: float) -> dict[str, float]:
    rest = (1 - weight) / max(1, len(keys) - 1)
    return {key: round(weight if key == chosen else rest, 4) for key in keys}


def _decide(body: dict[str, Any]) -> dict[str, Any]:
    """模拟判断模型：每个问题按键名回答（purchase、intent、concern、emotion、human、route）。"""
    state = str(body.get("state") or "")
    text = _latest_customer(state)
    answers: dict[str, Any] = {}
    for key, question in (body.get("questions") or {}).items():
        criteria = question.get("criteria")
        if key == "purchase":
            level = _purchase_level(text)
            levels = [str(i) for i in range(len(criteria or []) or 5)]
            probabilities = _spread(str(level), levels, 0.82)
            score = sum(int(k) * v for k, v in probabilities.items())
            answers[key] = {
                "type": "score",
                "score": round(score, 4),
                "confidence": 0.82,
                "probabilities": probabilities,
            }
        elif key == "intent":
            options = dict(criteria or {})
            customs = {k: str(v).split("：", 1)[0] for k, v in options.items() if k.startswith("x")}
            code = _pick_intent(text, customs, set(options))
            choice = code if code in options else "other"
            answers[key] = {
                "type": "choice",
                "choice": choice,
                "confidence": 0.8,
                "probabilities": _spread(choice, list(options), 0.8),
            }
        elif key == "concern":
            options = list(dict(criteria or {}))
            found = _concerns(text)
            probabilities = dict.fromkeys(options, 0.02)
            if found:
                for code in found:
                    probabilities[code] = round(0.8 / len(found), 4)
            else:
                probabilities["none"] = 0.9
            choice = found[0] if found else "none"
            answers[key] = {
                "type": "choice",
                "choice": choice,
                "confidence": 0.8,
                "probabilities": probabilities,
            }
        elif key == "emotion":
            level = _emotion(text)
            answers[key] = {
                "type": "score",
                "score": float(level),
                "confidence": 0.9,
                "probabilities": _spread(str(level), ["0", "1", "2"], 0.9),
            }
        elif key == "human" or question.get("type") == "noul":
            # 要人工；运营后台"检查连通"问的是"客户在询问价格"。
            about_price = "价格" in str(question.get("instructions") or "")
            asked = any(w in text for w in _HUMAN_WORDS) or (
                about_price and ("多少钱" in state or "价格" in state)
            )
            answers[key] = {"type": "noul", "noul": 0.95 if asked else 0.03}
        elif key == "route":
            names = [name for name in dict(criteria or {}) if name != "其他"]
            choice = _route(text, names)
            answers[key] = {
                "type": "choice",
                "choice": choice,
                "confidence": 0.85,
                "probabilities": _spread(choice, [*names, "其他"], 0.85),
            }
    questions = len(body.get("questions") or {})
    return {
        "model": "jev-fake-1",
        "answers": answers,
        "usage": {"input_tokens": (len(state) // 2 + 60) * questions, "output_tokens": 0},
    }


def _intent_llm(system: str, state: str) -> str:
    """对话模型做意图判断（没有判断模型时）：同样的规则，按提示词要求输出 JSON。"""
    text = _latest_customer(state)
    section = system.split("【真实意图】", 1)[-1].split("【在意什么】", 1)[0]
    codes = [line.split("：", 1)[0] for line in section.splitlines()[1:] if "：" in line]
    customs = {code: code.removeprefix("x:") for code in codes if code.startswith("x:")}
    routes_line = system.split("【分配意图】", 1)[-1].splitlines()
    names = routes_line[1].split("、") if "【分配意图】" in system and len(routes_line) > 1 else []
    data: dict[str, Any] = {
        "purchase": _purchase_level(text),
        "purchase_confidence": 0.8,
        "intent": _pick_intent(text, customs, set(codes)),
        "intent_confidence": 0.8,
        "concerns": _concerns(text),
        "emotion": _emotion(text),
        "human": 0.95 if any(w in text for w in _HUMAN_WORDS) else 0.05,
    }
    if names:
        data["route"] = _route(text, names)
    return json.dumps(data, ensure_ascii=False)


def _wake_brief(report: str) -> str:
    lines = [line.strip() for line in report.splitlines() if line.strip()]
    total = next((line for line in lines if line.startswith("待处理的问题：")), "")
    first = next((line for line in lines if line.startswith("1. ")), "")
    if not first:
        return "今天没有发现需要处理的问题。"
    count = total.removeprefix("待处理的问题：").split("（")[0]
    return f"今天{count}问题待处理。最要紧的是{first[3:]}，建议负责人今天处理完。"


_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(个工作日|工作日|小时|天|日|元|%)")


def _kb_align(user: str) -> str:
    knowledge, _, policies = user.partition("【现行制度】")
    knowledge = knowledge.removeprefix("【知识】").strip()
    answer = knowledge.split("答案：", 1)[1].strip() if "答案：" in knowledge else knowledge
    mine = _AMOUNT.findall(answer)
    for sentence in re.split(r"[。；\n]", policies):
        for number, unit in _AMOUNT.findall(sentence):
            for k_number, k_unit in mine:
                if unit != k_unit or number == k_number:
                    continue
                fixed = re.sub(
                    rf"{re.escape(k_number)}\s*{re.escape(k_unit)}",
                    f"{number} {unit}",
                    answer,
                    count=1,
                )
                return json.dumps(
                    {
                        "verdict": "conflict",
                        "reason": f"知识里是 {k_number} {k_unit}，制度规定 {number} {unit}",
                        "clause": sentence.strip()[:100],
                        "answer": fixed,
                    },
                    ensure_ascii=False,
                )
    return json.dumps(
        {"verdict": "consistent", "reason": "", "clause": "", "answer": ""}, ensure_ascii=False
    )


def _kb_gap(section: str) -> str:
    pairs = []
    for sentence in re.split(r"[。；\n]", section):
        sentence = sentence.strip()
        if not sentence or not _AMOUNT.search(sentence):
            continue
        if "：" in sentence:
            subject, rule = sentence.split("：", 1)
            question = f"{subject.strip('# ')}是怎么规定的？"
        else:
            subject, rule = sentence[:12], sentence
            question = f"关于{subject}有什么规定？"
        pairs.append({"question": question, "answer": rule.strip() + "。"})
        if len(pairs) >= 2:
            break
    return json.dumps({"qa_pairs": pairs}, ensure_ascii=False)


def _section(user: str, name: str) -> str:
    """提示词里【name】这一段的内容。"""
    match = re.search(rf"【{name}】\n?(.*?)(?=\n\n【|\Z)", user, re.S)
    return match.group(1).strip() if match else ""


def _contract(user: str) -> str:
    """起草合同：有模板时照抄模板、填上需求里的交货天数；没有模板时按固定的条款起草，
    售后条款引用第一条规章制度。"""
    requirement = _section(user, "需求")
    template = _section(user, "模板")
    references = _section(user, "参考资料")
    days = re.search(r"(\d+)\s*天(?:内)?交货", requirement)
    values = {"交货期限": f"合同签订后 {days.group(1)} 天内"} if days else {}
    # 规章制度里讲质保的一句（找不到时用默认的条款）。
    warranty_rule = ""
    used: list[int] = []
    for block in references.split("\n\n"):
        head, _, content = block.partition("\n")
        number = re.match(r"\[(\d+)\] 【规章制度】", head)
        if not number:
            continue
        for line in content.split("\n"):
            if "质保" in line and "。" in line:
                warranty_rule = line.strip()
                used = [int(number.group(1))]
                break
        if used:
            break
    title = "定制加工合同" if "定制" in requirement else "购销合同"
    if template:
        body = template
        notes = ["已按模板起草，请核对模板里没填的内容"]
    else:
        warranty = warranty_rule or "质保期内非人为损坏免费维修。"
        payment = "预付 30% 定金，验收合格后付清余款。" if "30%" in requirement else "{{付款约定}}"
        body = "\n".join(
            [
                f"# {title}",
                "合同编号：{{合同编号}}    签订日期：{{签订日期}}",
                "甲方（需方）：{{客户名称}}    联系电话：{{客户电话}}",
                "乙方（供方）：{{我方名称}}",
                "## 一、标的",
                "{{标的清单}}",
                "## 二、价款与支付",
                "合同金额：人民币 {{合同金额}} 元（{{合同金额大写}}）。",
                f"付款方式：{payment}",
                "## 三、交付与验收",
                "交货期限：{{交货期限}}。",
                "## 四、售后与质保",
                warranty,
                "## 五、违约责任",
                "任何一方违约，应承担由此给对方造成的损失。",
                "## 六、争议解决",
                "协商不成的，提交乙方所在地人民法院诉讼解决。",
                "## 七、签署",
                "甲方（盖章）：{{客户名称}}    乙方（盖章）：{{我方名称}}",
            ]
        )
        notes = ["请核对交货期限和付款方式"]
    return json.dumps(
        {"title": title, "body": body, "values": values, "notes": notes, "used": used},
        ensure_ascii=False,
    )


PROSPECT_WANTS = ("要", "想", "买", "多少钱", "价格", "规格", "有货")
PROSPECT_CONCERNS = (
    (("贵", "优惠", "便宜"), "价格"),
    (("考虑", "商量"), "还要考虑"),
    (("比较", "别家"), "在和别家比较"),
    (("交期", "多久"), "交期"),
)


def _prospect(user: str) -> str:
    """整理意向客户：想要什么、顾虑、几天后跟进。"""
    said = [
        line.removeprefix("客户：").strip()
        for line in _section(user, "对话").splitlines()
        if line.startswith("客户：")
    ]
    wants = [t for t in said if any(w in t for w in PROSPECT_WANTS)]
    interest = (wants or said or [""])[0]
    concerns = [
        label for words, label in PROSPECT_CONCERNS if any(w in t for t in said for w in words)
    ]
    days = 3
    for text in said:
        if match := re.search(r"(\d+)\s*天后", text):
            days = int(match.group(1))
        elif "下周" in text:
            days = 7
        elif "明天" in text:
            days = 1
    return json.dumps(
        {"interest": interest[:60], "concerns": "、".join(concerns), "follow_days": days},
        ensure_ascii=False,
    )


def _prospect_message(user: str) -> str:
    """意向客户跟进话术：问候、想要什么，有参考资料时引用第一条的第一句。"""
    customer = re.search(r"【客户】(.*)", user)
    interest = re.search(r"【想要什么】(.*)", user)
    name = customer.group(1).strip() if customer else "您"
    wanted = interest.group(1).strip() if interest else ""
    if not wanted or wanted.startswith("（"):
        wanted = "产品"
    text = f"{name}您好！上次您问到的{wanted}，我们一直给您留意着。"
    used: list[int] = []
    references = _section(user, "参考资料")
    first = re.match(r"\[(\d+)\] [^\n]*\n([^。\n]*。?)", references)
    if first:
        text += first.group(2)
        used = [int(first.group(1))]
    text += "您看什么时候方便，我给您发一份详细的报价？"
    return json.dumps({"text": text, "used": used}, ensure_ascii=False)


def rerank_score(query: str, document: str) -> float:
    wanted = set(terms(query))
    if not wanted:
        return 0.0
    return round(len(wanted & set(terms(document))) / len(wanted), 4)


@dataclass
class FakeLLM:
    mode: str = "normal"
    requests: list[dict[str, Any]] = field(default_factory=list)
    # 回复里带的意图（提示词要求判断意图时）。
    intent: str | None = None
    # 请求带工具时按顺序返回的工具调用：(工具名, 参数)。
    tool_plan: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # 判断模型（/systemone）：down 时返回 503。
    judge_mode: str = "normal"

    def chat(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.requests.append(body)
        if self.mode == "down":
            return 503, {"error": {"message": "service unavailable"}}
        messages = body.get("messages") or []
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        task = system.split("\n", 1)[0]
        if task == TASK_ASSISTANT and body.get("tools") and not self.tool_plan:
            # 助理：按员工的话选工具，拿到结果后不再调用。
            offered = {t["function"]["name"] for t in body["tools"]}
            asked = not any(m.get("role") == "tool" for m in messages)
            planned = _assistant_call(last_user, offered) if asked else None
            if planned is not None:
                self.tool_plan = [planned]
        if task in (TASK_REPLY, TASK_ASSISTANT) and body.get("tools") and self.tool_plan:
            name, arguments = self.tool_plan.pop(0)
            return 200, self._completion(
                body,
                messages,
                None,
                tool_calls=[
                    {
                        "id": f"call_{len(self.requests)}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments, ensure_ascii=False),
                        },
                    }
                ],
            )
        if task == TASK_REPLY:
            outputs = [str(m.get("content") or "") for m in messages if m.get("role") == "tool"]
            content = self._reply(system, last_user, "\n".join(outputs), outputs[-1:])
        elif task == TASK_REWRITE:
            content = _rewrite(last_user)
        elif task == TASK_SESSION_SUMMARY:
            content = _session_summary(last_user)
        elif task == TASK_PHRASE:
            content = _phrases(last_user)
        elif task == TASK_SUMMARY:
            customer = [
                line.removeprefix("客户：")
                for line in last_user.splitlines()
                if line.startswith("客户：")
            ]
            content = f"客户咨询：{'；'.join(customer[-3:])[:100]}。"
        elif task == TASK_TODO_EXTRACT:
            content = "这不是 JSON" if self.mode == "bad_json" else _todo_extract(system, last_user)
        elif task == TASK_ORDER_EXTRACT:
            content = "这不是 JSON" if self.mode == "bad_json" else _order_extract(last_user)
        elif task == TASK_EXTRACT:
            content = "这不是 JSON" if self.mode == "bad_json" else _extract(last_user)
        elif task == TASK_GROUP_EXTRACT:
            content = "这不是 JSON" if self.mode == "bad_json" else _group_extract(last_user)
        elif task == TASK_ASSISTANT:
            content = _assistant_reply(messages, last_user)
        elif task == TASK_INTENT:
            content = "这不是 JSON" if self.mode == "bad_json" else _intent_llm(system, last_user)
        elif task == TASK_WAKE_BRIEF:
            content = _wake_brief(last_user)
        elif task == TASK_KB_ALIGN:
            content = "这不是 JSON" if self.mode == "bad_json" else _kb_align(last_user)
        elif task == TASK_KB_GAP:
            content = "这不是 JSON" if self.mode == "bad_json" else _kb_gap(last_user)
        elif task == TASK_CONTRACT:
            content = "这不是 JSON" if self.mode == "bad_json" else _contract(last_user)
        elif task == TASK_PROSPECT:
            content = "这不是 JSON" if self.mode == "bad_json" else _prospect(last_user)
        elif task == TASK_PROSPECT_MESSAGE:
            content = _prospect_message(last_user)
        elif task == TASK_SUGGEST:
            answers = [*_products(system), *_answers(system)] or ["您好，我帮您确认一下，请稍等。"]
            content = json.dumps({"suggestions": answers[:3]}, ensure_ascii=False)
        else:
            content = "好的。"
        return 200, self._completion(body, messages, content)

    def _completion(
        self,
        body: dict[str, Any],
        messages: list[dict[str, Any]],
        content: str | None,
        *,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        prompt = sum(len(str(m.get("content") or "")) for m in messages)
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        completion = len(content or "") // 2 + (20 if tool_calls else 0)
        return {
            "id": "fake",
            "object": "chat.completion",
            "model": body.get("model") or MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt // 2,
                "completion_tokens": completion,
                "total_tokens": prompt // 2 + completion,
            },
        }

    def _reply(
        self, system: str, question: str, tool_results: str = "", last: list[str] | None = None
    ) -> str:
        if self.mode == "bad_json":
            return "这不是 JSON"
        answers = _answers(system)
        if not answers and "答：" in tool_results:
            answers = [tool_results.split("答：", 1)[1].split("\n", 1)[0].strip()]
        # 待办、订单工具（按最后一次工具调用的结果）：按工具返回的话术答复、追问缺少的信息、
        # 复述订单、复述查到的商品或进度。
        latest = (last or [""])[0]
        if not answers and "请这样答复客户：" in latest:
            answers = [latest.rsplit("请这样答复客户：", 1)[1].split("\n", 1)[0].strip()]
        elif not answers and "还不能登记：" in latest:
            missing = latest.rsplit("还不能登记：", 1)[1].split("。", 1)[0]
            answers = [f"好的，还需要您补充：{missing}。"]
        elif not answers and "请客户确认：" in latest:
            recap = latest.rsplit("请客户确认：", 1)[1].split("\n", 1)[0].strip()
            answers = [f"您要的是：{recap}。确认的话请回复「确认」。"]
        elif not answers and "还不能提交：缺少" in latest:
            missing = latest.rsplit("还不能提交：缺少", 1)[1].split("。", 1)[0]
            answers = [f"好的，还需要您提供：{missing}。"]
        elif not answers and "对应多个商品：" in latest:
            choices = latest.rsplit("对应多个商品：", 1)[1].split("。", 1)[0]
            answers = [f"请问您要的是哪一款：{choices}？"]
        elif not answers and "商品库里没有找到" in latest:
            answers = ["抱歉，暂时没有找到您说的商品，您可以换个说法或者告诉我型号。"]
        elif not answers and "查到这些商品" in latest:
            answers = [re.sub(r"^\d+\. ", "", latest.strip().splitlines()[1])]
        elif not answers and "请告诉客户：" in latest:
            answers = [latest.rsplit("请告诉客户：", 1)[1].split("\n", 1)[0].strip()]
        elif not answers and "请告诉客户" in latest:
            answers = ["好的，已经在为您跟进，请耐心等待。"]
        elif not answers and "」：" in latest:
            answers = [latest.strip().splitlines()[0]]
        elif not answers and _products(system):
            answers = [_products(system)[0]]
        if self.mode == "handoff":
            reply = {
                "reply": "这个问题需要人工处理。",
                "confidence": 0.9,
                "handoff": True,
                "reason": "需要人工确认",
            }
        elif self.mode == "promise":
            reply = {
                "reply": "我们保证全额赔偿您的损失。",
                "confidence": 0.9,
                "handoff": False,
                "reason": "",
            }
        elif answers:
            reply = {"reply": answers[0], "confidence": 0.9, "handoff": False, "reason": ""}
        else:
            reply = {
                "reply": "抱歉，这个问题我暂时无法回答。",
                "confidence": 0.2,
                "handoff": True,
                "reason": "资料中没有相关内容",
            }
        if self.intent and '"intent"' in system:
            reply["intent"] = self.intent
        return json.dumps(reply, ensure_ascii=False)

    def embeddings(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.requests.append(body)
        if self.mode == "down":
            return 503, {"error": {"message": "service unavailable"}}
        inputs = body.get("input") or []
        if isinstance(inputs, str):
            inputs = [inputs]
        return 200, {
            "object": "list",
            "model": body.get("model") or EMBED_MODEL,
            "data": [
                {"object": "embedding", "index": i, "embedding": embed_text(text)}
                for i, text in enumerate(inputs)
            ],
            "usage": {"prompt_tokens": sum(len(t) for t in inputs) // 2},
        }

    def transcribe(self, raw: bytes) -> tuple[int, dict[str, Any]]:
        if self.mode == "down":
            return 503, {"error": {"message": "service unavailable"}}
        match = re.search(rb"text=([^\r\n]+)", raw)
        text = match.group(1).decode(errors="ignore") if match else "（语音内容）"
        return 200, {"text": text}

    def rerank(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.requests.append(body)
        if self.mode == "down":
            return 503, {"error": {"message": "service unavailable"}}
        query = str(body.get("query") or "")
        documents = [str(d) for d in body.get("documents") or []]
        results = [
            {"index": i, "relevance_score": rerank_score(query, doc)}
            for i, doc in enumerate(documents)
        ]
        results.sort(key=lambda r: r["relevance_score"], reverse=True)
        return 200, {"model": body.get("model") or "fake-rerank", "results": results}

    def decide(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.requests.append(body)
        if self.judge_mode == "down":
            return 503, {"error": {"message": "service unavailable"}}
        if not isinstance(body.get("questions"), dict) or not body.get("model"):
            return 422, {"error": {"message": "invalid request"}}
        return 200, _decide(body)

    def handle(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if method == "POST" and path.endswith("/chat/completions"):
            return self.chat(body)
        if method == "POST" and path.endswith("/systemone"):
            return self.decide(body)
        if method == "POST" and path.endswith("/embeddings"):
            return self.embeddings(body)
        if method == "POST" and path.endswith("/rerank"):
            return self.rerank(body)
        if method == "POST" and path.endswith("/_control"):
            if "judge_mode" in body and "mode" not in body:
                self.judge_mode = str(body.get("judge_mode") or "normal")
                return 200, {"judge_mode": self.judge_mode}
            self.mode = str(body.get("mode") or "normal")
            # 验收脚本安排接下来的工具调用：[["save_lead_info", {...}], ...]
            if isinstance(body.get("tool_plan"), list):
                self.tool_plan = [(str(n), dict(a)) for n, a in body["tool_plan"]]
            return 200, {"mode": self.mode, "tool_plan": len(self.tool_plan)}
        return 404, {"error": {"message": "not found"}}

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/audio/transcriptions"):
                status, payload = self.transcribe(request.content)
                return httpx.Response(status, json=payload)
            body = json.loads(request.content or b"{}")
            status, payload = self.handle(request.method, request.url.path, body)
            return httpx.Response(status, json=payload)

        return httpx.MockTransport(handler)

    def asgi(self) -> Any:
        fake = self

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] != "http":
                return
            raw = b""
            while True:
                message = await receive()
                raw += message.get("body", b"")
                if not message.get("more_body"):
                    break
            if scope["path"].endswith("/audio/transcriptions"):
                status, payload = fake.transcribe(raw)
            else:
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    body = {}
                status, payload = fake.handle(scope["method"], scope["path"], body)
            data = json.dumps(payload, ensure_ascii=False).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": data})

        return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="模拟的 OpenAI 兼容大模型服务")
    parser.add_argument("--port", type=int, default=8900)
    args = parser.parse_args()
    uvicorn.run(FakeLLM().asgi(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
