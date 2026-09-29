"""语音转文字（可选，设计文档 §9.3）：让 AI 理解客户发来的语音。

使用 OpenAI 兼容的 /audio/transcriptions 接口（multipart：file、model），返回 {"text": "..."}。
没有配置 EDP_ASR_BASE_URL 时不启用。
"""

import logging

import httpx

logger = logging.getLogger(__name__)


class AsrError(Exception):
    pass


class AsrClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def transcribe(self, data: bytes, filename: str, content_type: str) -> str:
        try:
            response = await self._http.post(
                "/audio/transcriptions",
                data={"model": self.model},
                files={"file": (filename, data, content_type)},
            )
        except httpx.HTTPError as exc:
            raise AsrError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise AsrError(f"HTTP {response.status_code}: {response.text[:200]}")
        try:
            text = response.json().get("text")
        except ValueError as exc:
            raise AsrError("response is not json") from exc
        return str(text or "").strip()
