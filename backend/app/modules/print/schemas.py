from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

PrinterBrandValue = Literal["xpyun", "feie"]
UseValue = Literal["order", "requisition"]
TicketKindValue = Literal["order", "requisition", "test"]
JobStatusValue = Literal["queued", "sent", "printed", "retrying", "dead"]
JobSourceValue = Literal["claim", "assign", "requisition", "manual", "test"]
PrinterStatusValue = Literal["unknown", "online", "offline", "abnormal", "misconfigured"]


class PrinterOut(BaseModel):
    id: UUID
    name: str
    brand: PrinterBrandValue
    brand_label: str
    account: str
    sn: str
    device_key: str
    uses: list[UseValue] = Field(description="自动打印的小票：order 加工单、requisition 领料单")
    copies: int
    enabled: bool
    status: PrinterStatusValue
    status_label: str
    status_checked_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class PrinterList(BaseModel):
    items: list[PrinterOut]


class PrinterIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    brand: PrinterBrandValue
    account: str = Field(
        min_length=1,
        max_length=128,
        description="厂商开放平台的开发者账号（芯烨云是开发者 ID，飞鹅云是注册账号）",
    )
    key: str | None = Field(
        default=None,
        max_length=128,
        description="开发者密钥（芯烨云 UserKEY、飞鹅云 UKEY）；修改时留空表示不改",
    )
    sn: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$", description="打印机编号"
    )
    device_key: str = Field(
        default="", max_length=64, description="飞鹅云机身标签上的 KEY；芯烨云不用"
    )
    uses: list[UseValue] = Field(default_factory=list)
    copies: int = Field(default=1, ge=1, le=3)
    enabled: bool = True


class PrinterOption(BaseModel):
    id: UUID
    name: str
    status: PrinterStatusValue
    status_label: str


class PrinterOptions(BaseModel):
    items: list[PrinterOption] = Field(description="启用的打印机")
    auto: bool = Field(description="有打印机会自动打印这种小票")


class PrintRequest(BaseModel):
    printer_id: UUID | None = Field(
        default=None, description="指定打印机；不指定时用设置了这种用途的打印机"
    )


class PrintJobOut(BaseModel):
    id: UUID
    printer_id: UUID | None
    printer_name: str
    kind: TicketKindValue
    kind_label: str
    ref_id: UUID | None
    ref_no: str
    seq: int = Field(description="第几次打印")
    copies: int
    source: JobSourceValue
    source_label: str
    requested_by: UUID | None
    requested_by_name: str
    status: JobStatusValue
    status_label: str
    attempts: int
    last_error: str | None
    cloud_order_id: str | None
    created_at: datetime
    sent_at: datetime | None
    printed_at: datetime | None
    content: str = Field(description="小票的纯文本预览（等宽字体显示）")


class PrintJobPage(BaseModel):
    items: list[PrintJobOut]
    total: int


class PrintResult(BaseModel):
    jobs: list[PrintJobOut] = Field(description="排进队列的打印任务（每台打印机一条）")
