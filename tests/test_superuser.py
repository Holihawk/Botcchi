"""超管设施：请求审批、广播/撤回、作弊器、好友/群组列表、退群。"""

import asyncio

import pytest
from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, IncomingRequest, SessionKey
from botcchi.config import PermsSettings, SocialSettings
from botcchi.db.engine import Database
from botcchi.perms import PermissionGate
from botcchi.social.service import SocialService
from botcchi.superuser import SuperuserService

SU = "900000"


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/su.db")
    await database.init()
    yield database
    await database.aclose()


@pytest.fixture
def perms():
    return PermissionGate(PermsSettings(), [SU])


@pytest.fixture
def social(db):
    return SocialService(SocialSettings(), db)


@pytest.fixture
def service(perms, db, social):
    svc = SuperuserService(perms, db=db, social=social)
    svc.broadcast_delay = 0
    return svc


class SuChannel(FakeChannel):
    """带超管设施能力的假 channel。"""

    def __init__(self):
        super().__init__()
        self.groups = [("111", "群一"), ("222", "群二"), ("777", "本群")]
        self.friends = [("10001", "hhk"), ("10002", "阿波")]
        self.decisions: list[tuple[str, str, bool]] = []
        self.recalled: list[str] = []
        self.left: list[str] = []
        self.images: list[tuple[SessionKey, str]] = []
        self.pending: list[IncomingRequest] = []
        """协议端侧待办申请（可疑好友申请等推送收不到的）"""
        self.fetch_calls = 0
        self.connected = True
        self._mid = 0

    @property
    def is_connected(self):
        return self.connected

    async def send_text(self, session, text, *, reply_to=None, at_user=None):
        await super().send_text(session, text, reply_to=reply_to, at_user=at_user)
        self._mid += 1
        return str(self._mid)

    async def send_image(self, session, file):
        self.images.append((session, file))
        self._mid += 1
        return str(self._mid)

    async def list_groups(self):
        return self.groups

    async def list_friends(self):
        return self.friends

    async def leave_group(self, group_id):
        self.left.append(group_id)
        return True

    async def set_request_result(self, req, approve):
        self.decisions.append((req.kind, req.flag, approve))
        return True

    async def fetch_pending_requests(self):
        self.fetch_calls += 1
        return list(self.pending)

    async def recall(self, message_id):
        self.recalled.append(message_id)
        return True


def msg(
    text: str,
    user_id: str = SU,
    chat_type: str = "group",
    mentions: list[str] | None = None,
) -> IncomingMessage:
    chat_id = "777" if chat_type == "group" else user_id
    return IncomingMessage(
        session=SessionKey(channel="fake", chat_type=chat_type, chat_id=chat_id),
        user_id=user_id,
        user_name="hhk",
        text=text,
        mentions=mentions or [],
    )


async def test_request_flow(service):
    channel = SuChannel()
    # 好友申请 + 拉群邀请入队，超管收到私聊通知
    await service.on_request(
        channel,
        IncomingRequest(
            kind="friend", flag="f1", user_id="10001",
            user_name="张三", comment="加个好友",
        ),
    )
    await service.on_request(
        channel,
        IncomingRequest(
            kind="group", flag="g1", user_id="10002", user_name="李四",
            sub_type="invite", group_id="333", group_name="奇怪的群",
        ),
    )
    notify = [t for s, t, _ in channel.sent if s.chat_id == SU]
    assert len(notify) == 2
    assert "张三" in notify[0] and "同意请求" in notify[0]
    assert "拉群" in notify[1] and "奇怪的群" in notify[1]

    assert await service.handle(msg("查看请求"), channel)
    listing = channel.sent[-1][1]
    assert "#1" in listing and "#2" in listing and "待处理请求（2）" in listing

    # 同意好友：channel 收到审批调用，队列出清
    assert await service.handle(msg("同意请求 1"), channel)
    assert channel.decisions == [("friend", "f1", True)]
    assert "已同意" in channel.sent[-1][1]
    # 拒绝拉群邀请——奇怪的群进不来
    assert await service.handle(msg("拒绝请求 2"), channel)
    assert channel.decisions[-1] == ("group", "g1", False)
    await service.handle(msg("查看请求"), channel)
    assert "没有待处理的请求" in channel.sent[-1][1]

    # 已处理/不存在的编号
    await service.handle(msg("同意请求 1"), channel)
    assert "没有待处理的请求 #1" in channel.sent[-1][1]
    await service.handle(msg("同意请求 abc"), channel)
    assert "用法" in channel.sent[-1][1]


async def test_doubt_friend_requests_are_polled_not_pushed(service):
    """可疑好友申请不会推事件，只能主动补捞。"""
    channel = SuChannel()
    channel.pending = [
        IncomingRequest(
            kind="friend", sub_type="doubt", flag="d1", user_id="2235087166",
            user_name="Mahiro", comment="群里看到的",
        )
    ]
    # 「查看请求」先补捞再列出——推送没来过，队列本来是空的
    await service.handle(msg("查看请求"), channel)
    listing = channel.sent[-1][1]
    assert "待处理请求（1）" in listing
    assert "[可疑好友] Mahiro(2235087166)" in listing and "群里看到的" in listing

    # 重复补捞按 flag 去重，不会冒出第二条
    await service.handle(msg("查看请求"), channel)
    assert "待处理请求（1）" in channel.sent[-1][1]

    # 协议端只支持同意；拒绝要给出可执行的说法，且保持待办
    await service.handle(msg("拒绝请求 1"), channel)
    assert "只能同意或忽略" in channel.sent[-1][1]
    assert channel.decisions == []
    await service.handle(msg("同意请求 1"), channel)
    assert channel.decisions == [("friend", "d1", True)]

    # 已处理的不再被补捞回来
    await service.handle(msg("查看请求"), channel)
    assert "没有待处理的请求" in channel.sent[-1][1]


async def test_watch_requests_notifies_superuser(service):
    """轮询发现新申请时主动私聊超管。"""
    channel = SuChannel()
    channel.pending = [
        IncomingRequest(
            kind="friend", sub_type="doubt", flag="d9", user_id="555",
            user_name="陌生人",
        )
    ]
    service.poll_interval = 0.01
    stop = asyncio.Event()
    task = asyncio.create_task(service.watch_requests(channel, stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task

    notify = [t for s, t, _ in channel.sent if s.chat_id == SU]
    assert len(notify) == 1  # 去重后只通知一次，不会每轮重复打扰
    assert "陌生人(555)" in notify[0] and "同意请求" in notify[0]


async def test_watch_requests_waits_for_connection(service):
    """反向 WS 还没连上时不发起查询，连上后立刻补第一次。"""
    channel = SuChannel()
    channel.connected = False
    channel.pending = [
        IncomingRequest(
            kind="friend", sub_type="doubt", flag="d9", user_id="555",
            user_name="陌生人",
        )
    ]
    service.poll_interval = 0.01
    service.connect_retry = 0.01
    stop = asyncio.Event()
    task = asyncio.create_task(service.watch_requests(channel, stop))

    await asyncio.sleep(0.05)
    assert channel.fetch_calls == 0  # 空窗期一次都不查，不会刷「没有可用连接」

    channel.connected = True
    await asyncio.sleep(0.05)
    stop.set()
    await task

    assert channel.fetch_calls > 0
    assert any("陌生人(555)" in t for s, t, _ in channel.sent if s.chat_id == SU)


async def test_request_ignore_and_clear(service):
    channel = SuChannel()
    for i in range(3):
        await service.on_request(
            channel,
            IncomingRequest(kind="friend", flag=f"f{i}", user_id=str(i)),
        )
    await service.handle(msg("忽略请求 1"), channel)
    assert "已忽略" in channel.sent[-1][1]
    await service.handle(msg("清空请求"), channel)
    assert "已清空 2 条" in channel.sent[-1][1]
    assert channel.decisions == []  # 忽略/清空不触发协议端审批


async def test_broadcast_and_recall(service):
    channel = SuChannel()
    # 群里发起：跳过本群，其余两个群各收到一条
    assert await service.handle(msg("广播 大家好呀"), channel)
    got = [(s.chat_id, t) for s, t, _ in channel.sent if s.chat_id != "777"]
    assert got == [("111", "大家好呀"), ("222", "大家好呀")]
    assert "已发送到 2 个群" in channel.sent[-1][1]

    # 撤回上一次广播
    assert await service.handle(msg("广播撤回"), channel)
    assert len(channel.recalled) == 2
    await service.handle(msg("广播撤回"), channel)
    assert "没有可撤回" in channel.sent[-1][1]

    # 空内容给用法提示，不发送
    before = len(channel.sent)
    await service.handle(msg("广播"), channel)
    assert "用法" in channel.sent[-1][1]
    assert len(channel.sent) == before + 1


async def test_broadcast_quoted_and_images(service):
    channel = SuChannel()
    # 引用消息广播：引用内容作为正文
    quoted = msg("【被引用的消息|阿波】明晚八点团建\n广播", chat_type="private")
    assert await service.handle(quoted, channel)
    texts = [t for s, t, _ in channel.sent if s.chat_type == "group"]
    assert texts.count("明晚八点团建") == 3  # 私聊发起 → 所有群

    # 带图广播
    m = msg("广播 看图", chat_type="private")
    m.images = ["https://example.com/a.jpg"]
    await service.handle(m, channel)
    assert len(channel.images) == 3
    assert channel.images[0][1] == "https://example.com/a.jpg"


async def test_cheat_commands(service, social):
    channel = SuChannel()
    assert await service.handle(
        msg("金币设置 500", mentions=["10001"]), channel
    )
    assert "设置为 500" in channel.sent[-1][1]
    assert (await social.peek("10001")).coins == 500

    # 纯 QQ 号形式 + 好感度小数
    assert await service.handle(msg("好感度设置 12.5 10002"), channel)
    assert "12.5" in channel.sent[-1][1]
    assert (await social.peek("10002")).impression == 12.5

    await service.handle(msg("金币设置 abc"), channel)
    assert "用法" in channel.sent[-1][1]
    await service.handle(msg("金币设置 12.5", mentions=["10001"]), channel)
    assert "用法" in channel.sent[-1][1]  # 金币不收小数


async def test_lists_and_leave(service):
    channel = SuChannel()
    await service.handle(msg("好友列表"), channel)
    assert "好友列表（2）" in channel.sent[-1][1]
    assert "hhk（10001）" in channel.sent[-1][1]
    await service.handle(msg("群组列表"), channel)
    assert "群组列表（3）" in channel.sent[-1][1]

    assert await service.handle(msg("退群 222"), channel)
    assert channel.left == ["222"]
    # 「退群」后面不是群号：不消费（可能是普通聊天）
    assert not await service.handle(msg("退群提示怎么改"), channel)


async def test_non_superuser_passthrough(service):
    channel = SuChannel()
    for text in ("查看请求", "广播 hello", "金币设置 1 @x", "群组列表"):
        assert not await service.handle(msg(text, user_id="10001"), channel)
    assert channel.sent == []


async def test_integration_via_chat_service(mock_openai, tmp_path):
    from botcchi.app import build_context

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    settings.onebot.superusers = [SU]
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()
    ctx.service.superuser.broadcast_delay = 0

    channel = SuChannel()
    await ctx.service.handle(msg("群组列表"), channel)
    assert "群组列表（3）" in channel.sent[-1][1]
    # 非超管发同样的指令不产生回应（落入闲聊触发，群概率为 0）
    before = len(channel.sent)
    await ctx.service.handle(msg("群组列表", user_id="10001"), channel)
    assert len(channel.sent) == before
    await ctx.shutdown()
