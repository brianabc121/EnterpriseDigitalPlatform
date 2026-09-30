"""模拟的 OpenAI 兼容大模型服务：单元测试里作为 httpx 传输层，浏览器验收时作为独立服务运行。

- /v1/embeddings：按词项（中文二元组）哈希到 1024 维再归一化，意思相近的问题向量相近。
- /v1/chat/completions：按系统提示第一行的任务名作答：
  - 在线客服回复：取【参考资料】第一条的答案作为回复（有资料时把握 0.9）；
    没有资料时回复无法回答并请求转人工。请求带了工具且 tool_plan 里有安排时，先返回工具调用，
    拿到工具结果后再回复（工具查到的资料也可以作为答案）。
  - 问题改写：按问号、分号、换行拆开，去掉寒暄；有上文且问题很短时补上上一句客户消息。
  - 转人工摘要：概括最后几句客户消息。
  - 坐席建议回复：把参考资料的答案作为建议。
  - 会话小结：客户说过的话作为诉求，带"退"字时标签为售后。
  - 知识提炼：客户的问题与紧跟的客服回答组成问答；客服没能解答的问题记为缺口。
  - 优秀话术：坐席说的较长的话。
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
from typing import Any

import httpx

from app.modules.ai.prompts import (
    NO_REFERENCE,
    TASK_EXTRACT,
    TASK_PHRASE,
    TASK_REPLY,
    TASK_REWRITE,
    TASK_SESSION_SUMMARY,
    TASK_SUGGEST,
    TASK_SUMMARY,
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
            tool_results = "\n".join(
                str(m.get("content") or "") for m in messages if m.get("role") == "tool"
            )
            content = self._reply(system, last_user, tool_results)
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
        elif task == TASK_EXTRACT:
            content = "这不是 JSON" if self.mode == "bad_json" else _extract(last_user)
        elif task == TASK_SUGGEST:
            answers = _answers(system) or ["您好，我帮您确认一下，请稍等。"]
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

    def _reply(self, system: str, question: str, tool_results: str = "") -> str:
        if self.mode == "bad_json":
            return "这不是 JSON"
        answers = _answers(system)
        if not answers and "答：" in tool_results:
            answers = [tool_results.split("答：", 1)[1].split("\n", 1)[0].strip()]
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
