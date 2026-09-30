"""提示词版本管理（运营后台，设计文档 §11.5）：为每个场景保存新版本、启用或回滚到任意版本、
改回内置模板。启用后几秒内所有进程生效（见 ai/prompt_store.py），同时清空各租户的答案缓存。
"""

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import NotFound, Unprocessable
from app.core.ids import new_id
from app.modules.ai import answer_cache
from app.modules.ai.prompts import BUILTIN, PROMPT_KEYS, TEMPLATE_VARIABLES, unknown_variables
from app.modules.audit.service import record_audit
from app.modules.platform.models import PromptTemplate
from app.modules.platform.schemas import (
    PromptOut,
    PromptVersionCreate,
    PromptVersionOut,
)


def _check_key(key: str) -> None:
    if key not in PROMPT_KEYS:
        raise NotFound("没有这个场景的提示词")


async def list_prompts(session: AsyncSession) -> list[PromptOut]:
    rows = (
        await session.scalars(
            select(PromptTemplate).order_by(PromptTemplate.key, PromptTemplate.version.desc())
        )
    ).all()
    by_key: dict[str, list[PromptTemplate]] = {}
    for row in rows:
        by_key.setdefault(row.key, []).append(row)
    return [_out(key, by_key.get(key, [])) for key in PROMPT_KEYS]


def _out(key: str, rows: list[PromptTemplate]) -> PromptOut:
    active = next((r.version for r in rows if r.active), None)
    return PromptOut(
        key=key,
        name=PROMPT_KEYS[key],
        variables=list(TEMPLATE_VARIABLES.get(key, ())),
        builtin=BUILTIN[key],
        active_version=active,
        versions=[
            PromptVersionOut(
                version=r.version,
                content=r.content,
                note=r.note,
                active=r.active,
                created_at=r.created_at,
            )
            for r in rows
        ],
    )


async def get_prompt(session: AsyncSession, key: str) -> PromptOut:
    _check_key(key)
    rows = (
        await session.scalars(
            select(PromptTemplate)
            .where(PromptTemplate.key == key)
            .order_by(PromptTemplate.version.desc())
        )
    ).all()
    return _out(key, list(rows))


async def _activate(session: AsyncSession, key: str, version: int | None) -> None:
    await session.execute(
        update(PromptTemplate).where(PromptTemplate.key == key).values(active=False)
    )
    if version is not None:
        await session.flush()
        await session.execute(
            update(PromptTemplate)
            .where(PromptTemplate.key == key, PromptTemplate.version == version)
            .values(active=True)
        )


async def create_version(
    ctx: AppContext,
    session: AsyncSession,
    key: str,
    payload: PromptVersionCreate,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
) -> PromptOut:
    _check_key(key)
    unknown = unknown_variables(key, payload.content)
    if unknown:
        allowed = "、".join(f"{{{v}}}" for v in TEMPLATE_VARIABLES.get(key, ())) or "（无）"
        raise Unprocessable(
            f"模板里有不支持的变量：{'、'.join(unknown)}。这个场景可以使用的变量：{allowed}"
        )
    latest = await session.scalar(
        select(func.max(PromptTemplate.version)).where(PromptTemplate.key == key)
    )
    version = (latest or 0) + 1
    session.add(
        PromptTemplate(
            id=new_id(),
            key=key,
            version=version,
            content=payload.content.strip(),
            note=payload.note.strip() if payload.note else None,
            created_by=actor_id,
        )
    )
    await session.flush()
    if payload.activate:
        await _activate(session, key, version)
    record_audit(
        session,
        action="prompt.version",
        actor_type="platform",
        actor_id=actor_id,
        resource_type="prompt",
        resource_id=f"{key}@{version}",
        detail={"key": key, "version": version, "activate": payload.activate},
        ip=ip,
    )
    await session.commit()
    if payload.activate:
        await _after_activation(ctx)
    return await get_prompt(session, key)


async def activate(
    ctx: AppContext,
    session: AsyncSession,
    key: str,
    version: int | None,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
) -> PromptOut:
    _check_key(key)
    if version is not None:
        exists = await session.scalar(
            select(PromptTemplate.id).where(
                PromptTemplate.key == key, PromptTemplate.version == version
            )
        )
        if exists is None:
            raise NotFound("版本不存在")
    await _activate(session, key, version)
    record_audit(
        session,
        action="prompt.activate",
        actor_type="platform",
        actor_id=actor_id,
        resource_type="prompt",
        resource_id=f"{key}@{version if version is not None else 'builtin'}",
        detail={"key": key, "version": version},
        ip=ip,
    )
    await session.commit()
    await _after_activation(ctx)
    return await get_prompt(session, key)


async def _after_activation(ctx: AppContext) -> None:
    ctx.prompts.invalidate()
    await answer_cache.clear_all(ctx)
