"""自定义列类型。"""

from typing import Any

from sqlalchemy.engine import Dialect
from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType[list[float]]):
    """pgvector 的 vector(n) 列。asyncpg 以文本格式收发，形如 "[0.1,0.2]"。"""

    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(self, **_: Any) -> str:
        return f"vector({self.dim})"

    def bind_processor(self, dialect: Dialect) -> Any:
        def process(value: list[float] | None) -> str | None:
            if value is None:
                return None
            return "[" + ",".join(f"{x:.7g}" for x in value) + "]"

        return process

    def result_processor(self, dialect: Dialect, coltype: Any) -> Any:
        def process(value: str | None) -> list[float] | None:
            if value is None:
                return None
            return [float(x) for x in value.strip("[]").split(",") if x]

        return process


def vector_literal(values: list[float]) -> str:
    """查询参数用的向量文本（配合 CAST(:v AS vector) 使用）。"""
    return "[" + ",".join(f"{x:.7g}" for x in values) + "]"
