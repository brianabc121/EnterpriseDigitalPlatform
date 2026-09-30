from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.modules.history.document import Changes, Doc

RecordTypeValue = Literal["order", "requisition", "receipt", "todo", "goods", "material"]


class FieldOut(BaseModel):
    key: str
    label: str
    value: str


class ColumnOut(BaseModel):
    key: str
    label: str


class RowOut(BaseModel):
    key: str
    cells: dict[str, str]


class TableOut(BaseModel):
    key: str
    label: str
    columns: list[ColumnOut]
    rows: list[RowOut]


class DocOut(BaseModel):
    fields: list[FieldOut]
    tables: list[TableOut]

    @classmethod
    def of(cls, doc: Doc) -> "DocOut":
        return cls(
            fields=[FieldOut(key=f.key, label=f.label, value=f.value) for f in doc.fields],
            tables=[
                TableOut(
                    key=t.key,
                    label=t.label,
                    columns=[ColumnOut(key=k, label=label) for k, label in t.columns],
                    rows=[RowOut(key=r.key, cells=r.cells) for r in t.rows],
                )
                for t in doc.tables
            ],
        )


class FieldChangeOut(BaseModel):
    key: str
    label: str
    before: str
    after: str


class CellChangeOut(BaseModel):
    key: str
    before: str
    after: str


class RowChangeOut(BaseModel):
    key: str
    cells: list[CellChangeOut]


class TableChangesOut(BaseModel):
    key: str
    added: list[str]
    removed: list[RowOut]
    changed: list[RowChangeOut]


class ChangesOut(BaseModel):
    fields: list[FieldChangeOut]
    tables: list[TableChangesOut]

    @classmethod
    def of(cls, changes: Changes) -> "ChangesOut":
        return cls(
            fields=[
                FieldChangeOut(key=c.key, label=c.label, before=c.before, after=c.after)
                for c in changes.fields
            ],
            tables=[
                TableChangesOut(
                    key=t.key,
                    added=t.added,
                    removed=[RowOut(key=r.key, cells=r.cells) for r in t.removed],
                    changed=[
                        RowChangeOut(
                            key=r.key,
                            cells=[
                                CellChangeOut(key=c.key, before=c.before, after=c.after)
                                for c in r.cells
                            ],
                        )
                        for r in t.changed
                    ],
                )
                for t in changes.tables
            ],
        )


class VersionOut(BaseModel):
    id: UUID
    seq: int
    action: str
    action_label: str = Field(description="主要操作的名称，例如新建、修改、确认")
    actions: list[str] = Field(description="同一次操作里的全部动作")
    actions_label: str = Field(description="同一次操作里的全部动作，例如“开单、确认”")
    actor_type: str
    actor_id: UUID | None
    actor_name: str
    reason: str | None
    note: str | None
    created_at: datetime
    summary: str = Field(description="和上一个版本相比的改动摘要")
    document: DocOut
    changes: ChangesOut = Field(description="和上一个版本相比的改动（第一个版本为空）")


class RecordHistory(BaseModel):
    record_type: RecordTypeValue
    type_label: str
    record_id: UUID
    label: str
    deleted: bool = Field(description="记录已经删除（最新的版本是删除）")
    complete: bool = Field(
        description="从新建开始都有记录；为 false 表示记录在启用修改历史之前创建，"
        "之前的修改没有记录"
    )
    versions: list[VersionOut] = Field(description="新的在前")


class HistoryFeedItem(BaseModel):
    id: UUID
    record_type: RecordTypeValue
    type_label: str
    record_id: UUID
    label: str
    seq: int
    action: str
    action_label: str
    actor_type: str
    actor_id: UUID | None
    actor_name: str
    reason: str | None
    note: str | None
    created_at: datetime
    summary: str


class HistoryFeed(BaseModel):
    items: list[HistoryFeedItem]
    next_cursor: str | None
