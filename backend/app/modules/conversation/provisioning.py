"""在 OpenIM 中开通平台对象：租户的系统用户和机器人、客户身份、服务群。

每一步都是幂等的。成功后在对象上记录完成时间（im_registered_at、im_ready_at），
由调用方决定何时提交；某一步失败时，下次访客初始化会从断点继续。
"""

import json
from datetime import UTC, datetime

from app.integrations.openim import IMUser, OpenIMClient
from app.modules.conversation import imids
from app.modules.conversation.models import Room
from app.modules.customer.models import CustomerIdentity

SYSTEM_NICKNAME = "系统消息"
BOT_NICKNAME = "智能客服"


class IMProvisioner:
    def __init__(self, im: OpenIMClient) -> None:
        self._im = im
        # 本进程内已确认开通系统用户的租户，避免每次访客初始化都多两次 REST 调用。
        self._ready_tenants: set[str] = set()

    async def ensure_tenant_users(self, tenant_code: str) -> None:
        if tenant_code in self._ready_tenants:
            return
        await self._im.ensure_users(
            [
                IMUser(imids.system_user(tenant_code), SYSTEM_NICKNAME),
                IMUser(imids.bot_user(tenant_code), BOT_NICKNAME),
            ]
        )
        self._ready_tenants.add(tenant_code)

    async def ensure_identity(self, identity: CustomerIdentity, *, nickname: str) -> None:
        if identity.im_registered_at is not None:
            return
        await self._im.ensure_users([IMUser(identity.im_user_id, nickname)])
        identity.im_registered_at = datetime.now(UTC)

    async def ensure_room(
        self, room: Room, identity: CustomerIdentity, *, tenant_code: str, group_name: str
    ) -> None:
        """服务群：群主是租户系统用户，初始成员是客户身份和机器人。"""
        if room.im_ready_at is not None:
            return
        await self.ensure_tenant_users(tenant_code)
        await self._im.ensure_group(
            group_id=room.im_group_id,
            name=group_name,
            owner_user_id=imids.system_user(tenant_code),
            member_user_ids=[identity.im_user_id, imids.bot_user(tenant_code)],
            ex=json.dumps({"room_id": str(room.id)}),
        )
        room.im_ready_at = datetime.now(UTC)
