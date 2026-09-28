import asyncpg
from sqlalchemy.exc import IntegrityError


def violated_unique_constraint(exc: IntegrityError) -> str | None:
    """返回被违反的唯一约束名；不是唯一约束冲突时返回 None。"""
    cause = getattr(exc.orig, "__cause__", None)
    if isinstance(cause, asyncpg.UniqueViolationError):
        name: str | None = cause.constraint_name
        return name
    return None
