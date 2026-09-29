from datetime import datetime

from pydantic import BaseModel, Field


class TenantKeyVersion(BaseModel):
    version: int
    created_at: datetime


class TenantKeys(BaseModel):
    versions: list[TenantKeyVersion]
    current: int | None = Field(description="当前加密使用的版本；为空表示还没有生成数据密钥")
    stale: int = Field(description="还没有换成当前版本加密的密文数量")


class KeyRotationResult(BaseModel):
    version: int
    reencrypted: dict[str, int] = Field(description="各表重新加密的密文数量")
    failed: int = Field(description="无法解密、没有重新加密的密文数量")


class RetentionPolicy(BaseModel):
    """聊天记录保留期（到期后由调度进程删除）。为空表示一直保留。"""

    messages_days: int | None = Field(
        default=None, ge=30, le=3650, description="聊天消息保留天数（到期删除消息和其中的文件）"
    )
    files_days: int | None = Field(
        default=None,
        ge=7,
        le=3650,
        description="聊天文件（图片、文件、语音）保留天数；到期只删除文件，消息里显示文件已过期",
    )
