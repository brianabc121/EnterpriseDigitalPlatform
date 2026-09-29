"""平台设置的读写：每个键保存一个 JSON 对象，读取时按模型校验，缺少或损坏时用默认值。"""

import uuid

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.platform.models import PlatformSetting


async def read[T: BaseModel](session: AsyncSession, key: str, model: type[T]) -> T:
    row = await session.get(PlatformSetting, key)
    try:
        return model.model_validate(row.value if row else {})
    except ValidationError:
        return model()


async def write(
    session: AsyncSession, key: str, value: BaseModel, *, actor_id: uuid.UUID | None
) -> None:
    """写入（不提交）。"""
    data = value.model_dump(mode="json")
    row = await session.get(PlatformSetting, key)
    if row is None:
        session.add(PlatformSetting(key=key, value=data, updated_by=actor_id))
    else:
        row.value = data
        row.updated_by = actor_id
