"""邮件渠道（设计文档 §10.8）：每个邮箱是一个渠道账号，IMAP 收信、SMTP 回复。"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, ForeignKeyConstraint, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Security(StrEnum):
    SSL = "ssl"  # 连接时就加密（IMAP 993、SMTP 465）
    STARTTLS = "starttls"  # 明文连接后升级为加密（IMAP 143、SMTP 587）
    NONE = "none"  # 不加密：只在允许内网地址的开发、测试环境可选


class MailStatus(StrEnum):
    ACTIVE = "active"  # 正常收信（最近一次可能失败，见 failures）
    PAUSED = "paused"  # 登录连续失败，暂停收信，等修改授权码或"立即收取"成功后恢复
    DISABLED = "disabled"  # 管理员停用：不收信，不能回复


class MailAccount(IdMixin, TimestampMixin, TenantMixin, Base):
    """一个邮箱。授权码用租户数据密钥加密保存（secret_enc）。"""

    __tablename__ = "mail_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "channel_account_id"),
        ForeignKeyConstraint(
            ["tenant_id", "channel_account_id"],
            ["channel_accounts.tenant_id", "channel_accounts.id"],
        ),
    )

    channel_account_id: Mapped[uuid.UUID]
    address: Mapped[str] = mapped_column(String(254))
    # 回复邮件时的发件人名称。
    display_name: Mapped[str] = mapped_column(String(64), server_default="")
    provider: Mapped[str] = mapped_column(String(16))
    imap_host: Mapped[str] = mapped_column(String(255))
    imap_port: Mapped[int]
    imap_security: Mapped[str] = mapped_column(String(8))
    smtp_host: Mapped[str] = mapped_column(String(255))
    smtp_port: Mapped[int]
    smtp_security: Mapped[str] = mapped_column(String(8))
    username: Mapped[str] = mapped_column(String(254))
    secret_enc: Mapped[str] = mapped_column(Text)
    # 回复邮件末尾的签名。
    signature: Mapped[str | None] = mapped_column(Text)
    # 不导入的发件人：完整地址或 "@域名"。
    ignore_senders: Mapped[list[str]] = mapped_column(ARRAY(String(254)), server_default="{}")
    status: Mapped[str] = mapped_column(String(12), server_default=MailStatus.ACTIVE.value)
    # 收信位置：收件箱的 UIDVALIDITY 和已经处理过的最后一个 UID。
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    last_uid: Mapped[int | None] = mapped_column(BigInteger)
    next_poll_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_polled_at: Mapped[datetime | None]
    last_received_at: Mapped[datetime | None]
    # 连续失败的次数（成功后清零）和最近一次的错误。
    failures: Mapped[int] = mapped_column(server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    # 没有导入的邮件数（自动回复、退信、群发、忽略的发件人）。
    ignored: Mapped[int] = mapped_column(server_default="0")
    created_by: Mapped[uuid.UUID | None]
