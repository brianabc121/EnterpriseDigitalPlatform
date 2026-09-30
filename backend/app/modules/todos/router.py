"""待办接口（设计文档 §16）：待办中心、待确认、处理操作、AI 预填、待办类型与待办设置。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES, Forbidden
from app.core.permissions import Permission
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.todos import actions, admin, extract, queries
from app.modules.todos.models import Priority, TodoSource, TodoStatus
from app.modules.todos.schemas import (
    AssignRequest,
    BatchRequest,
    BatchResult,
    CommentRequest,
    ConfirmRequest,
    DoneRequest,
    DueFilter,
    ExtractRequest,
    ExtractResult,
    MergeRequest,
    NoteRequest,
    NotifyRequest,
    NotifyResult,
    ReasonRequest,
    RejectRequest,
    RescheduleRequest,
    RevealOut,
    TodoCounts,
    TodoCreate,
    TodoDetail,
    TodoOut,
    TodoPage,
    TodoTypeList,
    TodoTypeOut,
    TodoTypeWrite,
    TodoUpdate,
    View,
)
from app.modules.todos.settings import TodoSettings

router = APIRouter(prefix="/api/v1", tags=["todos"], responses=ERROR_RESPONSES)

CanRead = Annotated[Principal, Depends(require_permission(Permission.TODO_READ))]
CanConfig = Annotated[Principal, Depends(require_permission(Permission.TODO_CONFIG))]
Context = Annotated[AppContext, Depends(get_context)]


@router.get("/todos", response_model=TodoPage)
async def list_todos(
    session: TenantDb,
    principal: CanRead,
    view: Annotated[
        View,
        Query(
            description="pending 待确认；mine 我的未完成；pool 我能认领的；assigned 我分派的；"
            "all 全部（默认不含待确认）"
        ),
    ] = "all",
    status: TodoStatus | None = None,
    type_id: UUID | None = None,
    priority: Priority | None = None,
    source: TodoSource | None = None,
    customer_id: UUID | None = None,
    session_id: UUID | None = None,
    assignee_id: UUID | None = None,
    due: Annotated[
        DueFilter | None, Query(description="overdue 已逾期；today 今日到期；soon 24 小时内到期")
    ] = None,
    q: Annotated[str | None, Query(max_length=64, description="按编号或标题搜索")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TodoPage:
    return await queries.list_todos(
        session,
        principal,
        view=view,
        status=status,
        type_id=type_id,
        priority=priority,
        source=source,
        customer_id=customer_id,
        session_id=session_id,
        assignee_id=assignee_id,
        due=due,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/todos/counts", response_model=TodoCounts)
async def counts(session: TenantDb, principal: CanRead) -> TodoCounts:
    """菜单角标：等我确认的、我的、今日到期、已逾期、我能认领的。"""
    return await queries.counts(session, principal)


@router.post("/todos", response_model=TodoOut, status_code=status.HTTP_201_CREATED)
async def create_todo(
    payload: TodoCreate, ctx: Context, session: TenantDb, principal: CanRead
) -> TodoOut:
    """员工新建（包括 AI 预填后核对保存的）：直接进入待办列表，按规则或指定的人分派。"""
    _handle(principal)
    todo = await actions.create(ctx, session, principal, payload)
    return await queries.out(session, todo)


@router.post("/todos/extract", response_model=ExtractResult)
async def extract_todos(
    payload: ExtractRequest, ctx: Context, session: TenantDb, principal: CanRead
) -> ExtractResult:
    """从选中的消息或粘贴的文字由 AI 预填待办（不保存）。"""
    _handle(principal)
    return await extract.prefill(ctx, session, principal, payload)


@router.post("/todos/batch", response_model=BatchResult)
async def batch(
    payload: BatchRequest, ctx: Context, session: TenantDb, principal: CanRead
) -> BatchResult:
    """批量确认或批量驳回待确认的待办。"""
    return await actions.batch(ctx, session, principal, payload)


@router.get("/todos/{todo_id}", response_model=TodoDetail)
async def get_todo(todo_id: UUID, session: TenantDb, principal: CanRead) -> TodoDetail:
    return await queries.detail(session, principal, todo_id)


@router.patch("/todos/{todo_id}", response_model=TodoOut)
async def update_todo(
    todo_id: UUID, payload: TodoUpdate, ctx: Context, session: TenantDb, principal: CanRead
) -> TodoOut:
    todo = await actions.update(ctx, session, principal, todo_id, payload)
    return await queries.out(session, todo)


@router.post("/todos/{todo_id}/reveal", response_model=RevealOut)
async def reveal(
    todo_id: UUID, request: Request, ctx: Context, session: TenantDb, principal: CanRead
) -> RevealOut:
    """查看敏感字段的完整内容（需要查看敏感信息的权限，记审计日志）。"""
    fields = await actions.reveal(ctx, session, principal, todo_id, ip=client_ip(request))
    return RevealOut(fields=fields)


@router.post("/todos/{todo_id}/confirm", response_model=TodoOut)
async def confirm(
    todo_id: UUID, payload: ConfirmRequest, ctx: Context, session: TenantDb, principal: CanRead
) -> TodoOut:
    todo = await actions.confirm(ctx, session, principal, todo_id, payload)
    return await queries.out(session, todo)


@router.post("/todos/{todo_id}/discard", response_model=TodoOut)
async def discard(
    todo_id: UUID, payload: RejectRequest, ctx: Context, session: TenantDb, principal: CanRead
) -> TodoOut:
    """驳回待确认的待办（选择原因）。"""
    todo = await actions.reject(ctx, session, principal, todo_id, payload)
    return await queries.out(session, todo)


@router.post("/todos/{todo_id}/merge", response_model=TodoOut)
async def merge(
    todo_id: UUID, payload: MergeRequest, ctx: Context, session: TenantDb, principal: CanRead
) -> TodoOut:
    todo = await actions.merge(ctx, session, principal, todo_id, payload)
    return await queries.out(session, todo)


@router.post("/todos/{todo_id}/claim", response_model=TodoOut)
async def claim(todo_id: UUID, ctx: Context, session: TenantDb, principal: CanRead) -> TodoOut:
    todo = await actions.claim(ctx, session, principal, todo_id)
    return await queries.out(session, todo)


@router.post("/todos/{todo_id}/start", response_model=TodoOut)
async def start(todo_id: UUID, ctx: Context, session: TenantDb, principal: CanRead) -> TodoOut:
    todo = await actions.start(ctx, session, principal, todo_id)
    return await queries.out(session, todo)


@router.post("/todos/{todo_id}/wait", response_model=TodoOut)
async def wait(
    todo_id: UUID, payload: NoteRequest, ctx: Context, session: TenantDb, principal: CanRead
) -> TodoOut:
    """等待客户补充信息（暂停计时）。"""
    todo = await actions.wait(ctx, session, principal, todo_id, payload.note)
    return await queries.out(session, todo)


@router.post("/todos/{todo_id}/resume", response_model=TodoOut)
async def resume(todo_id: UUID, ctx: Context, session: TenantDb, principal: CanRead) -> TodoOut:
    todo = await actions.resume(ctx, session, principal, todo_id)
    return await queries.out(session, todo)


@router.post("/todos/{todo_id}/done", response_model=TodoOut)
async def done(
    todo_id: UUID,
    payload: DoneRequest,
    response: Response,
    ctx: Context,
    session: TenantDb,
    principal: CanRead,
) -> TodoOut:
    """完成：填写处理结果，可以同时通知客户（结果在响应头 X-Customer-Notice）。"""
    todo, notice = await actions.complete(ctx, session, principal, todo_id, payload)
    if notice is not None:
        response.headers["X-Customer-Notice"] = notice.status
    return await queries.out(session, todo)


@router.post("/todos/{todo_id}/cancel", response_model=TodoOut)
async def cancel(
    todo_id: UUID, payload: ReasonRequest, ctx: Context, session: TenantDb, principal: CanRead
) -> TodoOut:
    todo = await actions.cancel(ctx, session, principal, todo_id, payload.reason)
    return await queries.out(session, todo)


@router.post("/todos/{todo_id}/reopen", response_model=TodoOut)
async def reopen(
    todo_id: UUID, payload: ReasonRequest, ctx: Context, session: TenantDb, principal: CanRead
) -> TodoOut:
    todo = await actions.reopen(ctx, session, principal, todo_id, payload.reason)
    return await queries.out(session, todo)


@router.post("/todos/{todo_id}/assign", response_model=TodoOut)
async def assign(
    todo_id: UUID,
    payload: AssignRequest,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanRead,
) -> TodoOut:
    """分派、改派，或把自己的待办转交他人。"""
    todo = await actions.assign_to(ctx, session, principal, todo_id, payload, ip=client_ip(request))
    return await queries.out(session, todo)


@router.post("/todos/{todo_id}/reschedule", response_model=TodoOut)
async def reschedule(
    todo_id: UUID,
    payload: RescheduleRequest,
    ctx: Context,
    session: TenantDb,
    principal: CanRead,
) -> TodoOut:
    todo = await actions.reschedule(ctx, session, principal, todo_id, payload)
    return await queries.out(session, todo)


@router.post("/todos/{todo_id}/comments", response_model=TodoOut)
async def comment(
    todo_id: UUID, payload: CommentRequest, ctx: Context, session: TenantDb, principal: CanRead
) -> TodoOut:
    todo = await actions.comment(ctx, session, principal, todo_id, payload)
    return await queries.out(session, todo)


@router.post("/todos/{todo_id}/notify", response_model=NotifyResult)
async def notify_customer(
    todo_id: UUID, payload: NotifyRequest, ctx: Context, session: TenantDb, principal: CanRead
) -> NotifyResult:
    """按客户所在渠道的规则通知客户（结果记入待办动态）。"""
    return await actions.notify_customer(ctx, session, principal, todo_id, payload.text)


def _handle(principal: Principal) -> None:
    if not (principal.has(Permission.TODO_HANDLE) or principal.has(Permission.TODO_ASSIGN)):
        raise Forbidden("没有处理待办的权限")


# ---- 待办类型与设置 ----


@router.get("/todo-types", response_model=TodoTypeList)
async def list_types(session: TenantDb, principal: CanRead) -> TodoTypeList:
    return await admin.list_types(session, principal.tenant_id)


@router.post("/admin/todo-types", response_model=TodoTypeOut, status_code=status.HTTP_201_CREATED)
async def create_type(
    payload: TodoTypeWrite, request: Request, session: TenantDb, principal: CanConfig
) -> TodoTypeOut:
    return await admin.create_type(session, principal, payload, ip=client_ip(request))


@router.put("/admin/todo-types/{type_id}", response_model=TodoTypeOut)
async def update_type(
    type_id: UUID,
    payload: TodoTypeWrite,
    request: Request,
    session: TenantDb,
    principal: CanConfig,
) -> TodoTypeOut:
    return await admin.update_type(session, principal, type_id, payload, ip=client_ip(request))


@router.delete("/admin/todo-types/{type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_type(
    type_id: UUID, request: Request, session: TenantDb, principal: CanConfig
) -> Response:
    await admin.delete_type(session, principal, type_id, ip=client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/admin/todo-settings", response_model=TodoSettings)
async def get_settings(session: TenantDb, principal: CanConfig) -> TodoSettings:
    return await admin.get_settings(session, principal.tenant_id)


@router.put("/admin/todo-settings", response_model=TodoSettings)
async def put_settings(
    payload: TodoSettings, request: Request, session: TenantDb, principal: CanConfig
) -> TodoSettings:
    return await admin.put_settings(session, principal, payload, ip=client_ip(request))
