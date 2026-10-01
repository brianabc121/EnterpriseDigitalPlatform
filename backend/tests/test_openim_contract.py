"""OpenIM 客户端契约测试：同一组断言分别跑在内存版 OpenIM 和真实 OpenIM 上。

真实 OpenIM 只在设置了 EDP_TEST_OPENIM_URL 时参与（例如 make im-up 之后
EDP_TEST_OPENIM_URL=http://localhost:10002），用来保证 tests/fake_openim.py 与实际行为一致。
真实 OpenIM 建群前会回调宿主机 8000 端口上的后端；没有开发后端在跑时（CI 的后端测试作业），
测试自己应答回调（tests/openim_hooks.py）。端口可用 EDP_TEST_OPENIM_HOOK_PORT 改。
"""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from app.integrations.openim import (
    ContentType,
    ErrCode,
    IMUser,
    OpenIMClient,
    OpenIMError,
    OpenIMUnavailable,
    SeqRange,
    group_conversation_id,
)
from app.modules.conversation import hooks
from tests.fake_openim import SECRET, FakeOpenIM
from tests.openim_hooks import HookResponder

REAL_URL = os.environ.get("EDP_TEST_OPENIM_URL")
REAL_SECRET = os.environ.get("EDP_TEST_OPENIM_SECRET", "openim-dev-secret")
HOOK_PORT = int(os.environ.get("EDP_TEST_OPENIM_HOOK_PORT", "8000"))


@dataclass
class Backend:
    client: OpenIMClient
    fake: FakeOpenIM | None
    prefix: str
    hooks: HookResponder | None = None

    def uid(self, name: str) -> str:
        return f"{self.prefix}_{name}"

    @property
    def room(self) -> str:
        # 符合平台服务群约定（{t}_r_{32 位十六进制}），开发环境的建群回调才会放行。
        return f"{self.prefix}_r_{'0' * 32}"


@pytest.fixture(
    params=[
        "fake",
        pytest.param(
            "real",
            marks=pytest.mark.skipif(not REAL_URL, reason="EDP_TEST_OPENIM_URL 未设置"),
        ),
    ]
)
async def backend(request: pytest.FixtureRequest) -> AsyncIterator[Backend]:
    prefix = f"ct{uuid.uuid4().hex[:10]}"
    if request.param == "fake":
        fake = FakeOpenIM()
        client = OpenIMClient("http://openim", secret=SECRET, transport=fake.transport())
        yield Backend(client, fake, prefix)
        await client.aclose()
        return
    assert REAL_URL is not None
    responder = HookResponder(HOOK_PORT)
    await responder.start()
    client = OpenIMClient(REAL_URL, secret=REAL_SECRET)
    try:
        yield Backend(client, None, prefix, hooks=responder)
    finally:
        await client.aclose()
        await responder.stop()


async def _room(b: Backend) -> str:
    await b.client.ensure_users(
        [IMUser(b.uid("sys"), "系统"), IMUser(b.uid("bot"), "AI"), IMUser(b.uid("c"), "访客")]
    )
    group_id = b.room
    await b.client.ensure_group(
        group_id=group_id,
        name="服务群",
        owner_user_id=b.uid("sys"),
        member_user_ids=[b.uid("c"), b.uid("bot")],
    )
    return group_id


async def test_ensure_users_is_idempotent_and_tolerates_partial_batches(backend: Backend) -> None:
    await backend.client.ensure_users([IMUser(backend.uid("a"), "A")])
    # 批次里有已注册用户时，OpenIM 会拒绝整批；客户端应只注册缺少的那个。
    await backend.client.ensure_users(
        [IMUser(backend.uid("a"), "A"), IMUser(backend.uid("b"), "B")]
    )
    await backend.client.ensure_users(
        [IMUser(backend.uid("a"), "A"), IMUser(backend.uid("b"), "B")]
    )
    token = await backend.client.get_user_token(backend.uid("b"))
    assert token.token
    assert token.expires_in > 0


async def test_user_ids_only_accept_letters_digits_and_underscores(backend: Backend) -> None:
    await backend.client.ensure_users([IMUser(backend.uid("Ok_09"), "ok")])
    for bad in ("with-hyphen", "with.dot", "with:colon"):
        with pytest.raises(OpenIMError) as excinfo:
            await backend.client.ensure_users([IMUser(backend.uid(bad), "bad")])
        assert excinfo.value.code == ErrCode.ARGS


async def test_ensure_group_is_idempotent(backend: Backend) -> None:
    await _room(backend)
    created_again = await backend.client.ensure_group(
        group_id=backend.room,
        name="服务群",
        owner_user_id=backend.uid("sys"),
        member_user_ids=[backend.uid("c")],
    )
    assert created_again is False
    if backend.hooks is not None and backend.hooks.active:
        # 真实 OpenIM 建群前确实回调了平台，平台的服务群规则放行了这个群。
        assert hooks.BEFORE_CREATE_GROUP in backend.hooks.commands


async def test_seq_and_pull(backend: Backend) -> None:
    group_id = await _room(backend)
    conversation_id = group_conversation_id(group_id)
    sent = await backend.client.send_group_message(
        send_id=backend.uid("c"),
        group_id=group_id,
        content_type=ContentType.TEXT,
        content={"content": "你好"},
        ex=json.dumps({"k": 1}),
    )
    # 在线信令不落库，不占用 seq。
    await backend.client.send_group_message(
        send_id=backend.uid("sys"),
        group_id=group_id,
        content_type=ContentType.CUSTOM,
        content={"data": "{}", "description": "", "extension": ""},
        online_only=True,
    )
    await backend.client.send_group_message(
        send_id=backend.uid("bot"),
        group_id=group_id,
        content_type=ContentType.TEXT,
        content={"content": "您好"},
    )

    seqs = await _wait_for_seq(backend, conversation_id, 3)
    assert seqs == {conversation_id: 3}

    pulled = await backend.client.pull_messages(
        backend.uid("sys"), SeqRange(conversation_id, 1, 50)
    )
    assert pulled.is_end
    assert [m.seq for m in pulled.messages] == [1, 2, 3]
    notification, first, second = pulled.messages
    # 建群通知占用第一个 seq。
    assert notification.is_notification
    assert first.server_msg_id == sent.server_msg_id
    assert first.send_id == backend.uid("c")
    assert json.loads(first.content) == {"content": "你好"}
    assert json.loads(first.ex) == {"k": 1}
    assert first.send_time == sent.send_time
    assert json.loads(second.content) == {"content": "您好"}

    partial = await backend.client.pull_messages(
        backend.uid("sys"), SeqRange(conversation_id, 2, 2)
    )
    assert [m.seq for m in partial.messages] == [2]
    assert not partial.is_end


async def test_non_members_see_max_seq_but_pull_nothing(backend: Backend) -> None:
    group_id = await _room(backend)
    conversation_id = group_conversation_id(group_id)
    await _wait_for_seq(backend, conversation_id, 1)
    await backend.client.ensure_users([IMUser(backend.uid("stranger"), "路人")])
    seqs = await backend.client.max_seqs(backend.uid("stranger"), [conversation_id])
    assert seqs == {conversation_id: 1}
    pulled = await backend.client.pull_messages(
        backend.uid("stranger"), SeqRange(conversation_id, 1, 10)
    )
    assert pulled.messages == []


async def test_group_membership_changes(backend: Backend) -> None:
    group_id = await _room(backend)
    staff = backend.uid("s_" + "1" * 32)
    await backend.client.ensure_users([IMUser(staff, "坐席")])

    await backend.client.add_group_members(group_id, [staff])
    # 已在群里时再次邀请不应出错（直接邀请会得到 500）。
    await backend.client.add_group_members(group_id, [staff, backend.uid("c")])
    assert await backend.client.group_member_ids(group_id, [staff]) == {staff}

    # 员工从未同步过这个会话：真实 OpenIM 会返回 1001，但成员已被移除。
    await backend.client.remove_group_members(group_id, [staff])
    assert await backend.client.group_member_ids(group_id, [staff]) == set()
    await backend.client.remove_group_members(group_id, [staff])


async def test_online_signals_need_friendship(backend: Backend) -> None:
    staff = backend.uid("s_" + "2" * 32)
    await backend.client.ensure_users([IMUser(backend.uid("sys"), "系统"), IMUser(staff, "坐席")])
    signal = {"data": "{}", "description": "edp.signal", "extension": ""}

    with pytest.raises(OpenIMError) as excinfo:
        await backend.client.send_online_only(
            send_id=backend.uid("sys"), recv_id=staff, content=signal
        )
    assert excinfo.value.code == 1303

    await backend.client.import_friends(backend.uid("sys"), [staff])
    await backend.client.import_friends(backend.uid("sys"), [staff])
    sent = await backend.client.send_online_only(
        send_id=backend.uid("sys"), recv_id=staff, content=signal
    )
    assert sent.server_msg_id


async def test_errors_carry_openim_codes(backend: Backend) -> None:
    with pytest.raises(OpenIMError) as excinfo:
        await backend.client.get_user_token(backend.uid("nobody"))
    # 实测：未注册用户换 token 返回 1004（RecordNotFound），而不是 1101。
    assert excinfo.value.code == ErrCode.RECORD_NOT_FOUND


async def test_admin_token_is_refreshed_after_rejection() -> None:
    fake = FakeOpenIM()
    client = OpenIMClient("http://openim", secret=SECRET, transport=fake.transport())
    await client.ensure_users([IMUser("t_a", "A")])
    fake.expire_tokens()
    await client.ensure_users([IMUser("t_b", "B")])
    assert fake.calls.count("/auth/get_admin_token") == 2
    await client.aclose()


async def test_network_failures_are_unavailable() -> None:
    fake = FakeOpenIM()
    fake.down = True
    client = OpenIMClient("http://openim", secret=SECRET, transport=fake.transport())
    with pytest.raises(OpenIMUnavailable):
        await client.ensure_users([IMUser("t_a", "A")])
    await client.aclose()


async def _wait_for_seq(backend: Backend, conversation_id: str, expected: int) -> dict[str, int]:
    """真实 OpenIM 异步分配 seq（经 Kafka），发送后要稍等才能看到。"""
    for _ in range(50):
        seqs = await backend.client.max_seqs(backend.uid("sys"), [conversation_id])
        if seqs.get(conversation_id, 0) >= expected:
            return seqs
        await asyncio.sleep(0.1)
    return seqs
