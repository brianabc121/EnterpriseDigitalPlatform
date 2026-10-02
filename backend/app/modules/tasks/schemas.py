import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.modules.tasks.models import TaskPriority, TaskSource, TaskStatus

TaskView = Literal["mine", "assigned", "all"]
TaskDueFilter = Literal["overdue", "today", "soon"]


class TaskAllowed(BaseModel):
    edit: bool = Field(description="当前员工能修改、完成、取消这条事项（主人、交办人或管理员）")


class TaskOut(BaseModel):
    id: uuid.UUID
    no: str
    owner_id: uuid.UUID
    owner_name: str | None
    title: str
    note: str
    priority: TaskPriority
    status: TaskStatus
    source: TaskSource
    due_at: datetime | None
    overdue: bool = Field(description="截止时间已过、还没完成")
    remind_before_minutes: int | None
    link: str | None
    done_note: str | None
    done_at: datetime | None
    created_by: uuid.UUID | None
    created_by_name: str | None
    created_at: datetime
    updated_at: datetime
    allowed: TaskAllowed


class TaskPage(BaseModel):
    items: list[TaskOut]
    total: int


class TaskCounts(BaseModel):
    open: int = Field(description="我的未完成事项")
    due_today: int
    overdue: int
    work_todos: int | None = Field(
        description="分派给我、未完成的客户待办（§24）；没有查看待办的权限时为空"
    )


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=4000)
    priority: TaskPriority = TaskPriority.NORMAL
    due_at: datetime | None = None
    remind_before_minutes: int | None = Field(
        default=None, ge=0, le=7 * 24 * 60, description="不填时按租户设置"
    )
    owner_id: uuid.UUID | None = Field(
        default=None, description="交办给谁；不填是自己。交办需要 task:assign"
    )
    link: str | None = Field(default=None, max_length=500, description="关联的控制台页面路径")


class TaskUpdate(BaseModel):
    """没有给出的字段不变；due_at 给 null 表示清除截止时间。"""

    title: str | None = Field(default=None, min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=4000)
    priority: TaskPriority | None = None
    due_at: datetime | None = None
    remind_before_minutes: int | None = Field(default=None, ge=0, le=7 * 24 * 60)
    link: str | None = Field(default=None, max_length=500)


class TaskDoneRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000, description="完成备注")


class TaskOverviewRow(BaseModel):
    staff_id: uuid.UUID
    name: str
    open: int
    due_today: int
    overdue: int


class TaskOverview(BaseModel):
    """全员视图（task:read_all）：每个在职员工的事项数量。"""

    items: list[TaskOverviewRow]


class StaffOption(BaseModel):
    id: uuid.UUID
    name: str


class StaffOptions(BaseModel):
    """可以交办的对象：启用状态的员工。"""

    items: list[StaffOption]
