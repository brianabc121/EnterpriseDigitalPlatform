"""当前租户：给 span、日志和 HTTP 指标打上租户维度。

- 处理一个 HTTP 请求时，中间件先建一个请求范围（begin_request）；鉴权依赖、访客依赖或回调确定租户后
  调用 note_tenant 记下，请求结束时中间件据此给指标和服务端 span 加租户标签。
- 处理事件、执行按租户的任务、打开租户数据库会话时用 bind_tenant 限定范围，期间创建的 span 和
  写出的日志都带这个租户。
- 触发操作的员工（大模型调用记到员工名下，设计文档 §37）：员工请求的鉴权依赖调用 note_staff
  记到请求上；不在员工请求里、但代表员工的处理（AI 助理回答机器人消息）用 bind_staff 限定范围。
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass
class RequestScope:
    tenant_id: uuid.UUID | None = None
    # 登录的员工和所属企业。
    staff: tuple[uuid.UUID, uuid.UUID] | None = None


_tenant: ContextVar[uuid.UUID | None] = ContextVar("edp_tenant", default=None)
_request: ContextVar[RequestScope | None] = ContextVar("edp_request", default=None)
_staff: ContextVar[tuple[uuid.UUID, uuid.UUID] | None] = ContextVar("edp_staff", default=None)


def current_tenant() -> uuid.UUID | None:
    tenant = _tenant.get()
    if tenant is None:
        scope = _request.get()
        tenant = scope.tenant_id if scope is not None else None
    return tenant


def note_tenant(tenant_id: uuid.UUID) -> None:
    """请求处理过程中确定了租户：记到当前请求上（第一次确定的为准）。"""
    scope = _request.get()
    if scope is not None and scope.tenant_id is None:
        scope.tenant_id = tenant_id


def note_staff(tenant_id: uuid.UUID, staff_id: uuid.UUID) -> None:
    """员工请求：记下登录的员工（第一次确定的为准）。"""
    scope = _request.get()
    if scope is not None and scope.staff is None:
        scope.staff = (tenant_id, staff_id)


def current_staff(tenant_id: uuid.UUID) -> uuid.UUID | None:
    """代表哪个员工在处理这个企业的事：bind_staff 绑定的优先，其次是请求里登录的员工。

    只认同一个企业的员工；访客请求、事件处理和定时任务里没有员工（系统）。
    """
    bound = _staff.get()
    if bound is None:
        scope = _request.get()
        bound = scope.staff if scope is not None else None
    if bound is None or bound[0] != tenant_id:
        return None
    return bound[1]


@contextmanager
def bind_staff(tenant_id: uuid.UUID, staff_id: uuid.UUID) -> Iterator[None]:
    """在这个范围内代表员工 staff_id 处理企业 tenant_id 的事。"""
    token = _staff.set((tenant_id, staff_id))
    try:
        yield
    finally:
        with suppress(ValueError):
            _staff.reset(token)


@contextmanager
def bind_tenant(tenant_id: uuid.UUID | None) -> Iterator[None]:
    """在这个范围内当前租户是 tenant_id。

    不改变请求所属的租户：平台运营的请求可能依次访问多个租户。
    """
    token = _tenant.set(tenant_id)
    try:
        yield
    finally:
        # 异步生成器在别的上下文里收尾时无法复位，忽略即可（上下文随任务结束一起丢弃）。
        with suppress(ValueError):
            _tenant.reset(token)


def begin_request() -> tuple[RequestScope, Token[RequestScope | None]]:
    scope = RequestScope()
    return scope, _request.set(scope)


def end_request(token: Token[RequestScope | None]) -> None:
    with suppress(ValueError):
        _request.reset(token)
