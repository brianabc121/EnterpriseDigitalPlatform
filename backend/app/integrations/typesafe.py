"""TypeSafe 判断模型（Jev，设计文档 §32.2）的客户端：``POST {base_url}/systemone``。

判断模型不写文字，只回答事先定义好的问题，给出概率。请求 ``{state, model, questions}``，返回
``{model, answers, usage}``：

- 是非（noul）：返回 ``noul``，是的概率（0–1）；
- 单选（choice）：``criteria`` 是 {选项: 说明}，返回 ``choice``、每个选项的 ``probabilities``
  和 ``confidence``；
- 刻度（score）：``criteria`` 是从低到高的各级说明，返回按概率加权的 ``score``（可以落在两级之间）、
  每一级的 ``probabilities``（{"0": p, ...}，也兼容数组）和 ``confidence``。

只按输入计费（每千 tokens 多少分）。超时、限流（429）、过载（529）和服务端错误重试一次，仍失败时抛出
LLMUnavailable，由调用方降级（不影响 AI 接待）。
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from app.core.errors import Unprocessable
from app.core.urls import resolve_outbound
from app.integrations.llm import LLMError, LLMUnavailable

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.typesafe.ai/v1"
DEFAULT_MODEL = "jev-latest"
_RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 529}

QuestionKind = Literal["noul", "choice", "score"]


@dataclass(frozen=True)
class JudgeEndpoint:
    """一家判断模型的接口。价格为每千 tokens 多少分（输出通常不计费）。"""

    base_url: str
    api_key: str
    model: str = DEFAULT_MODEL
    name: str = ""
    price_input: float = 0.0
    price_output: float = 0.0

    @property
    def provider(self) -> str:
        return self.name or urlsplit(self.base_url).netloc or "typesafe"

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.price_input + output_tokens * self.price_output) / 1000


@dataclass(frozen=True)
class Question:
    kind: QuestionKind
    instructions: str
    # 单选：{选项: 说明}；刻度：从低到高的各级说明；
    # 是非：{"true": 说明, "false": 说明}（可以不给）。
    criteria: dict[str, str] | list[str] | None = None

    def body(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.kind, "instructions": self.instructions}
        if self.criteria:
            data["criteria"] = self.criteria
        return data


@dataclass(frozen=True)
class Answer:
    kind: str
    # 是非：是的概率。
    probability: float | None = None
    # 单选：选中的选项。
    choice: str | None = None
    # 刻度：按概率加权的位置（0 到级数 - 1）。
    score: float | None = None
    # 单选：{选项: 概率}；刻度：{"0": 概率, "1": ...}。
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None


@dataclass(frozen=True)
class DecideResult:
    answers: dict[str, Answer]
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost: float = 0.0


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return max(0.0, min(1.0, float(value)))


def _probabilities(raw: Any) -> dict[str, float]:
    """{选项: 概率}；刻度的概率也可能是按级排列的数组。"""
    if isinstance(raw, list):
        raw = {str(index): value for index, value in enumerate(raw)}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, float] = {}
    for key, value in raw.items():
        number = _number(value)
        if number is not None:
            result[str(key)] = number
    return result


def parse_answer(raw: Any, question: Question) -> Answer | None:
    """解析一个回答；格式不对时为空（调用方当作没有回答）。"""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or question.kind).lower()
    probabilities = _probabilities(raw.get("probabilities"))
    confidence = _number(raw.get("confidence"))
    if question.kind == "noul":
        # 官方返回 noul；一些网关包装成 probability。
        probability = _number(raw.get("noul", raw.get("probability")))
        return None if probability is None else Answer(kind=kind, probability=probability)
    if question.kind == "choice":
        choice = raw.get("choice")
        options = question.criteria if isinstance(question.criteria, dict) else {}
        if not isinstance(choice, str) or choice not in options:
            # 没有给出选项时取概率最大的。
            known = {k: v for k, v in probabilities.items() if k in options}
            if not known:
                return None
            choice = max(known, key=lambda key: known[key])
        return Answer(kind=kind, choice=choice, probabilities=probabilities, confidence=confidence)
    levels = len(question.criteria) if isinstance(question.criteria, list) else 0
    score = raw.get("score")
    if isinstance(score, bool) or not isinstance(score, int | float):
        if not probabilities:
            return None
        score = sum(int(k) * v for k, v in probabilities.items() if k.isdigit())
    value = max(0.0, min(float(levels - 1) if levels else float(score), float(score)))
    return Answer(kind=kind, score=value, probabilities=probabilities, confidence=confidence)


class JudgeClient:
    def __init__(
        self,
        endpoint: JudgeEndpoint | None,
        *,
        timeout: float = 5.0,
        retries: int = 1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.retries = retries
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    @property
    def enabled(self) -> bool:
        return self.endpoint is not None

    async def decide(self, state: str, questions: dict[str, Question]) -> DecideResult:
        """一次请求回答所有问题。没有回答（或回答格式不对）的问题不出现在结果里。"""
        endpoint = self.endpoint
        if endpoint is None:
            raise LLMUnavailable("没有配置判断模型")
        body = {
            "state": state,
            "model": endpoint.model,
            "questions": {key: q.body() for key, q in questions.items()},
        }
        try:
            data, latency = await self._post(endpoint, body)
        except LLMError as exc:
            logger.warning("decision via %s failed: %s", endpoint.provider, exc)
            raise LLMUnavailable(f"{endpoint.provider}: {exc}") from exc
        raw_answers = data.get("answers")
        if not isinstance(raw_answers, dict):
            raise LLMUnavailable(f"{endpoint.provider}: 响应里没有 answers")
        answers: dict[str, Answer] = {}
        for key, question in questions.items():
            answer = parse_answer(raw_answers.get(key), question)
            if answer is not None:
                answers[key] = answer
        usage = data.get("usage") or {}
        tokens_in = int(usage.get("input_tokens") or 0) if isinstance(usage, dict) else 0
        tokens_out = int(usage.get("output_tokens") or 0) if isinstance(usage, dict) else 0
        return DecideResult(
            answers=answers,
            provider=endpoint.provider,
            model=str(data.get("model") or endpoint.model),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            latency_ms=latency,
            cost=endpoint.cost(tokens_in, tokens_out),
        )

    async def _post(
        self, endpoint: JudgeEndpoint, body: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        url = endpoint.base_url.rstrip("/") + "/systemone"
        headers = {"authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
        last = "unknown error"
        for attempt in range(self.retries + 1):
            if attempt:
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
            started = time.monotonic()
            try:
                # 判断模型的地址由平台运营配置（开发、验收时是本机的模拟服务）。
                target = await resolve_outbound(url, allow_private=True)
                response = await self._http.post(
                    target.request_url,
                    json=body,
                    headers={**headers, **target.headers},
                    extensions=target.extensions,
                )
            except Unprocessable as exc:
                raise LLMError(f"接口地址不可用：{exc.message}") from exc
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue
            latency = int((time.monotonic() - started) * 1000)
            if response.status_code == 200:
                try:
                    data = response.json()
                except json.JSONDecodeError as exc:
                    raise LLMError("响应不是 JSON") from exc
                if not isinstance(data, dict):
                    raise LLMError("响应格式不正确")
                return data, latency
            last = f"HTTP {response.status_code}: {response.text[:200]}"
            if response.status_code not in _RETRYABLE:
                break
        raise LLMError(last)

    async def aclose(self) -> None:
        await self._http.aclose()
