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
- /v1/rerank：问题词项被文档覆盖的比例作为相关度。
- /v1/audio/transcriptions：语音转文字。音频内容里带 "text=..." 时返回这段文字（测试和验收
  发的"语音"里写好要转写的内容），否则返回固定的文字。
- 可以切换模式模拟故障：down（503）、bad_json（不是 JSON）、promise（回复里带承诺类话术）、
  handoff（模型要求转人工）。独立运行时用 POST /_control {"mode": "down"} 切换；
  {"tool_plan": [["save_lead_info", {...}]]} 安排接下来的工具调用。

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

from app.modules.ai.prompts import (
    NO_REFERENCE,
    TASK_EXTRACT,
    TASK_ORDER_EXTRACT,
    TASK_PHRASE,
    TASK_REPLY,
    TASK_REWRITE,
    TASK_SESSION_SUMMARY,
    TASK_SUGGEST,
    TASK_SUMMARY,
    TASK_TODO_EXTRACT,
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

    def chat(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.requests.append(body)
        if self.mode == "down":
            return 503, {"error": {"message": "service unavailable"}}
        messages = body.get("messages") or []
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        task = system.split("\n", 1)[0]
        if task == TASK_REPLY and body.get("tools") and self.tool_plan:
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

    def handle(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if method == "POST" and path.endswith("/chat/completions"):
            return self.chat(body)
        if method == "POST" and path.endswith("/embeddings"):
            return self.embeddings(body)
        if method == "POST" and path.endswith("/rerank"):
            return self.rerank(body)
        if method == "POST" and path.endswith("/_control"):
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
