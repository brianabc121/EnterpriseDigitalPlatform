"""提示词版本的运行时读取（设计文档 §11.5）：运营在运营后台为各场景发布并启用新版本，没有启用的
版本时用 prompts.BUILTIN。启用的版本在进程内缓存几秒；每次调用把版本（如 reply@3、reply@builtin）
记到 llm_calls.prompt_version，便于对照效果和回滚。
"""

import asyncio
import time
from dataclasses import dataclass

from sqlalchemy import select

from app.db.session import Database
from app.modules.ai.prompts import BUILTIN

CACHE_SECONDS = 5.0


@dataclass(frozen=True)
class Prompt:
    key: str
    content: str
    version: str


class PromptStore:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._active: dict[str, tuple[int, str]] | None = None
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._active = None

    async def _load(self) -> dict[str, tuple[int, str]]:
        if self._active is not None and time.monotonic() - self._loaded_at < CACHE_SECONDS:
            return self._active
        async with self._lock:
            if self._active is not None and time.monotonic() - self._loaded_at < CACHE_SECONDS:
                return self._active
            from app.modules.platform.models import PromptTemplate

            async with self._db.platform_sessionmaker() as session:
                columns = (PromptTemplate.key, PromptTemplate.version, PromptTemplate.content)
                rows = await session.execute(
                    select(*columns).where(PromptTemplate.active.is_(True))
                )
                self._active = {key: (version, content) for key, version, content in rows}
            self._loaded_at = time.monotonic()
            return self._active

    async def get(self, key: str) -> Prompt:
        active = (await self._load()).get(key)
        if active is not None:
            version, content = active
            return Prompt(key=key, content=content, version=f"{key}@{version}")
        return Prompt(key=key, content=BUILTIN[key], version=f"{key}@builtin")
