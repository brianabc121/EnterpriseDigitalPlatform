"""待办的字段：按类型的定义校验（必填、格式、长度），敏感字段用租户数据密钥加密、默认掩码显示
（设计文档 §24.2、§24.8）。

保存的格式：普通字段 {key: 值}；敏感字段 {key: {"enc": 密文, "masked": 掩码}}。
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.modules.customer.sensitive import (
    mask_email,
    mask_phone,
    normalize_email,
    normalize_phone,
    valid_email,
    valid_phone,
)
from app.modules.security.keys import TenantKeyring
from app.modules.todos.models import FieldType, TodoType

TEXT_LIMIT = 500
FILE_LIMIT = 1000
KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# 能唯一标识一件事的字段：两条待办都有且相同时视为同一件事，不同时不合并。
IDENTITY_KEYS = ("order_no",)


@dataclass
class Cleaned:
    values: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    def problems(self) -> list[str]:
        return [*(f"缺少{label}" for label in self.missing), *self.errors.values()]


def validate_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验并规范化类型的字段定义，不合法时抛出 ValueError。"""
    result: list[dict[str, Any]] = []
    keys: set[str] = set()
    for spec in specs:
        key = str(spec.get("key") or "")
        if not KEY.match(key):
            raise ValueError(f"字段编码只能用小写字母、数字和下划线，以字母开头：{key}")
        if key in keys:
            raise ValueError(f"字段编码重复：{key}")
        keys.add(key)
        label = str(spec.get("label") or "").strip()[:32]
        if not label:
            raise ValueError(f"字段 {key} 缺少名称")
        try:
            type_ = FieldType(str(spec.get("type") or FieldType.TEXT))
        except ValueError as exc:
            raise ValueError(f"字段 {key} 的类型不支持：{spec.get('type')}") from exc
        item: dict[str, Any] = {
            "key": key,
            "label": label,
            "type": type_.value,
            "required": bool(spec.get("required")),
            "sensitive": bool(spec.get("sensitive")),
        }
        if type_ == FieldType.OPTION:
            options = [str(o).strip()[:32] for o in spec.get("options") or [] if str(o).strip()]
            if not options:
                raise ValueError(f"选项字段 {key} 至少需要一个选项")
            item["options"] = list(dict.fromkeys(options))
        result.append(item)
    if len(result) > 20:
        raise ValueError("每个类型最多 20 个字段")
    return result


def _clean_value(spec: dict[str, Any], raw: Any) -> str:
    """规范化一个字段的值，不合法时抛出 ValueError（说明给用户或模型看）。"""
    label = spec["label"]
    value = str(raw).strip()
    match spec["type"]:
        case FieldType.PHONE:
            phone = normalize_phone(value)
            if not valid_phone(phone):
                raise ValueError(f"{label}的格式不正确")
            return phone
        case FieldType.EMAIL:
            email = normalize_email(value)
            if not valid_email(email):
                raise ValueError(f"{label}的格式不正确")
            return email
        case FieldType.NUMBER:
            try:
                number = float(value.replace(",", ""))
            except ValueError as exc:
                raise ValueError(f"{label}应该是数字") from exc
            return str(int(number)) if number.is_integer() else str(round(number, 4))
        case FieldType.DATE:
            try:
                return date.fromisoformat(value[:10]).isoformat()
            except ValueError as exc:
                raise ValueError(f"{label}应该是日期（如 2026-10-01）") from exc
        case FieldType.OPTION:
            options = spec.get("options") or []
            if value not in options:
                raise ValueError(f"{label}只能是：{'、'.join(options)}")
            return value
        case FieldType.FILE:
            return value[:FILE_LIMIT]
        case _:
            return value[:TEXT_LIMIT]


def clean(type_: TodoType, values: dict[str, Any] | None, *, require: bool = True) -> Cleaned:
    """按类型的字段定义清理字段值：丢弃未定义的字段和空值，检查格式；require 时检查必填。"""
    specs = {spec["key"]: spec for spec in type_.fields or []}
    result = Cleaned()
    for key, raw in (values or {}).items():
        spec = specs.get(key)
        if spec is None or raw is None or not str(raw).strip():
            continue
        try:
            result.values[key] = _clean_value(spec, raw)
        except ValueError as exc:
            result.errors[key] = str(exc)
    if require:
        result.missing = [
            spec["label"]
            for key, spec in specs.items()
            if spec.get("required") and key not in result.values and key not in result.errors
        ]
    return result


def mask(type_: str, value: str) -> str:
    if type_ == FieldType.PHONE:
        return mask_phone(value)
    if type_ == FieldType.EMAIL:
        return mask_email(value)
    if type_ == FieldType.ADDRESS:
        return value[:6] + "****" if len(value) > 6 else "****"
    if len(value) <= 2:
        return "**"
    return value[0] + "*" * min(len(value) - 2, 8) + value[-1]


def _sensitive(type_: TodoType) -> dict[str, dict[str, Any]]:
    return {spec["key"]: spec for spec in type_.fields or [] if spec.get("sensitive")}


async def seal(
    keys: TenantKeyring | None, tenant_id: uuid.UUID, type_: TodoType, values: dict[str, str]
) -> dict[str, Any]:
    """转成保存的格式：敏感字段加密并附带掩码。"""
    sensitive = _sensitive(type_)
    stored: dict[str, Any] = {}
    for key, value in values.items():
        spec = sensitive.get(key)
        if spec is None:
            stored[key] = value
            continue
        if keys is None:
            raise RuntimeError("sealing a sensitive to-do field needs the tenant keyring")
        stored[key] = {
            "enc": await keys.seal(tenant_id, value),
            "masked": mask(spec["type"], value),
        }
    return stored


def is_sealed(value: Any) -> bool:
    return isinstance(value, dict) and "enc" in value


def masked(stored: dict[str, Any]) -> dict[str, str]:
    """显示用：敏感字段显示掩码。"""
    return {k: (v.get("masked", "****") if is_sealed(v) else str(v)) for k, v in stored.items()}


async def reveal(
    keys: TenantKeyring, tenant_id: uuid.UUID, stored: dict[str, Any]
) -> dict[str, str]:
    """全部字段的明文（查看敏感字段需要权限并记审计，由调用方负责）。"""
    result: dict[str, str] = {}
    for key, value in stored.items():
        result[key] = await keys.unseal(tenant_id, value["enc"]) if is_sealed(value) else str(value)
    return result


async def merge(
    keys: TenantKeyring | None,
    tenant_id: uuid.UUID,
    type_: TodoType,
    stored: dict[str, Any],
    changes: dict[str, str],
) -> dict[str, Any]:
    """修改部分字段（值为空字符串表示清除）。"""
    result = dict(stored)
    kept = {k: v for k, v in changes.items() if v}
    for key in changes:
        result.pop(key, None)
    result.update(await seal(keys, tenant_id, type_, kept))
    return result


def identity_values(stored: dict[str, Any]) -> dict[str, str]:
    return {k: str(stored[k]) for k in IDENTITY_KEYS if k in stored and not is_sealed(stored[k])}


def labelled(type_: TodoType | None, stored: dict[str, Any]) -> list[tuple[str, str, str]]:
    """（编码, 名称, 显示值）：按类型定义的顺序，类型里已经删除的字段排在最后。"""
    shown = masked(stored)
    specs = [spec for spec in (type_.fields if type_ else []) or [] if spec["key"] in shown]
    rows = [(spec["key"], spec["label"], shown[spec["key"]]) for spec in specs]
    known = {spec["key"] for spec in specs}
    rows += [(key, key, value) for key, value in shown.items() if key not in known]
    return rows


def parse_time(value: str | None, tz: Any, now: datetime) -> datetime | None:
    """客户期望的时间：ISO 时间（没有时区时按租户时区），只接受未来 30 天内的；否则为空。"""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            moment = datetime.combine(date.fromisoformat(value.strip()[:10]), datetime.min.time())
            moment = moment.replace(hour=18)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    if moment <= now or (moment - now).days >= 30:
        return None
    return moment
