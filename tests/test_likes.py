"""点赞：批量点赞、指令回复、回赞聚合致谢与奖励、通知事件解析。"""

import asyncio

import pytest
from test_service import FakeChannel, make_settings

from botcchi.app import build_context
from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.channels.onebot.adapter import OneBotAdapter
from botcchi.config import OneBotSettings, SocialSettings
from botcchi.db.archive import MessageArchive
from botcchi.db.engine import Database
from botcchi.social.likes import ALREADY_TEXTS, UNKNOWN_FAIL_TEXTS, LikeService
from botcchi.social.service import SocialService
from botcchi.social.tools import build_like_tool
from botcchi.tools.registry import ToolContext


class LikeChannel(FakeChannel):
    """send_like 前 N 批成功、之后失败（模拟 QQ 每日限额）。"""

    def __init__(
        self,
        succeed_batches: int = 1,
        friend: bool | None = True,
        friend_on_refresh: bool | None = None,
    ):
        super().__init__()
        self.succeed_batches = succeed_batches
        self.friend = friend
        self.friend_on_refresh = (
            friend if friend_on_refresh is None else friend_on_refresh
        )
        """强制重查时的结果（默认与 friend 一致）"""
        self.like_calls: list[tuple[str, int]] = []
        self.friend_checks: list[bool] = []
        """每次好友判定用的 refresh 取值"""

    async def send_like(self, user_id: str, times: int = 10) -> bool:
        self.like_calls.append((user_id, times))
        return len(self.like_calls) <= self.succeed_batches

    async def is_friend(
        self, user_id: str, *, refresh: bool = False
    ) -> bool | None:
        self.friend_checks.append(refresh)
        return self.friend_on_refresh if refresh else self.friend


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/likes.db")
    await database.init()
    yield database
    await database.aclose()


async def test_give_stops_on_failure():
    likes = LikeService()
    channel = LikeChannel(succeed_batches=1)
    assert (await likes.give(channel, "10001")).count == 10
    assert len(channel.like_calls) == 2  # 第二批失败即停

    channel = LikeChannel(succeed_batches=99)
    assert (await likes.give(channel, "10001")).count == 50  # 封顶 5 批

    result = await likes.give(FakeChannel(), "10001")  # 不支持点赞
    assert not result.supported

    channel = LikeChannel(friend=False)  # 非好友：不发起点赞调用
    result = await likes.give(channel, "10001")
    assert result.friend is False and result.count == 0
    assert channel.like_calls == []
    assert channel.friend_checks == [False, True]  # 拒绝前强制重查过一次


async def test_give_retries_friend_check_for_fresh_friend():
    """刚加上好友：首次判定命中旧缓存，强制重查后应正常点赞。"""
    likes = LikeService()
    channel = LikeChannel(succeed_batches=99, friend=False, friend_on_refresh=True)
    result = await likes.give(channel, "10001", times=10)
    assert result.friend is True and result.count == 10
    assert channel.friend_checks == [False, True]


async def test_like_command_replies(mock_openai, tmp_path):
    service = build_context(make_settings(tmp_path), client=mock_openai.client()).service
    session = SessionKey(channel="fake", chat_type="private", chat_id="10001")

    def msg(text: str) -> IncomingMessage:
        return IncomingMessage(
            session=session, user_id="10001", user_name="hhk", text=text, at_me=True
        )

    channel = LikeChannel(succeed_batches=1)
    await service.handle(msg("赞我"), channel)
    assert len(channel.sent) == 1 and "10" in channel.sent[0][1]

    channel = LikeChannel(succeed_batches=0)  # 好友但限额用尽 → 明确已赞过
    await service.handle(msg("点赞"), channel)
    assert channel.sent[0][1] in ALREADY_TEXTS

    channel = LikeChannel(friend=False)  # 非好友 → 明确要求加好友
    await service.handle(msg("点赞"), channel)
    assert "好友" in channel.sent[0][1]
    assert channel.like_calls == []

    channel = LikeChannel(succeed_batches=0, friend=None)  # 判定失败 → 模糊说法
    await service.handle(msg("点赞"), channel)
    assert channel.sent[0][1] in UNKNOWN_FAIL_TEXTS

    channel = FakeChannel()
    await service.handle(msg("给我点赞"), channel)
    assert "点不了赞" in channel.sent[0][1]


async def test_profile_like_aggregation_and_reward(db):
    social = SocialService(SocialSettings(), db)
    likes = LikeService(social=social, thanks_window=0.05)
    channel = LikeChannel()

    await likes.on_profile_like(channel, "10001", "hhk", 3)
    await likes.on_profile_like(channel, "10001", "hhk", 4)
    assert channel.sent == []  # 窗口内先聚合
    await asyncio.sleep(0.15)

    assert len(channel.sent) == 1
    session, text, _ = channel.sent[0]
    assert session.chat_type == "private" and session.chat_id == "10001"
    assert "hhk" in text and "7 个赞" in text and "金币" in text
    p = await social.peek("10001")
    assert p.coins >= 1 and p.impression >= 0.07


async def test_profile_like_threshold_flushes_immediately(db):
    social = SocialService(SocialSettings(), db)
    likes = LikeService(social=social, thanks_window=30.0)
    channel = LikeChannel()
    await likes.on_profile_like(channel, "10001", "hhk", 10)
    assert len(channel.sent) == 1  # 满 10 个不等窗口

    # 群里活跃过则致谢发到群
    archive = MessageArchive(db)
    await archive.add("777", "10001", "hhk", "冒个泡")
    likes2 = LikeService(social=social, archive=archive, thanks_window=30.0)
    await likes2.on_profile_like(channel, "10001", "hhk", 10)
    assert channel.sent[-1][0].chat_type == "group"
    assert channel.sent[-1][0].chat_id == "777"


async def test_adapter_dispatches_profile_like_notice():
    received: list[tuple[str, str, int]] = []

    async def on_like(user_id: str, nickname: str, times: int) -> None:
        received.append((user_id, nickname, times))

    adapter = OneBotAdapter(
        OneBotSettings(), on_message=None, on_profile_like=on_like  # type: ignore[arg-type]
    )
    adapter._dispatch(
        {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "profile_like",
            "operator_id": 654321,
            "operator_nick": "hhk",
            "times": 2,
        },
        self_id="999",
    )
    await asyncio.sleep(0)
    assert received == [("654321", "hhk", 2)]
    # 其他通知类型被忽略
    adapter._dispatch(
        {"post_type": "notice", "notice_type": "group_increase"}, self_id="999"
    )
    await asyncio.sleep(0)
    assert len(received) == 1


async def test_send_like_tool():
    likes = LikeService()
    tool = build_like_tool(likes)
    assert tool.needs_context

    channel = LikeChannel(succeed_batches=1)
    session = SessionKey(channel="fake", chat_type="private", chat_id="10001")
    ctx = ToolContext(
        incoming=IncomingMessage(
            session=session, user_id="10001", user_name="hhk", text="", at_me=True
        ),
        channel=channel,
        bot_name="波奇",
    )
    assert "10 个赞" in await tool.execute("", ctx)
    assert "点满" in await tool.execute("", ctx)  # 好友但限额用尽

    not_friend = ToolContext(
        incoming=ctx.incoming, channel=LikeChannel(friend=False), bot_name="波奇"
    )
    assert "还不是好友" in await tool.execute("", not_friend)


async def test_give_with_times():
    likes = LikeService()
    channel = LikeChannel(succeed_batches=99)
    assert (await likes.give(channel, "10001", times=5)).count == 5
    assert channel.like_calls == [("10001", 5)]

    channel = LikeChannel(succeed_batches=99)
    assert (await likes.give(channel, "10001", times=23)).count == 23
    assert channel.like_calls == [("10001", 10), ("10001", 10), ("10001", 3)]

    channel = LikeChannel(succeed_batches=99)
    assert (await likes.give(channel, "10001", times=999)).count == 50  # 封顶 50

    channel = LikeChannel(succeed_batches=1)  # 第二批起失败
    assert (await likes.give(channel, "10001", times=23)).count == 10


async def test_adapter_friend_lookup_and_cache(monkeypatch):
    adapter = OneBotAdapter(OneBotSettings(), on_message=None)  # type: ignore[arg-type]
    calls = []

    async def fake_call_api(action, params):
        calls.append((action, params))
        return {"status": "ok", "data": [{"user_id": 654321}]}

    monkeypatch.setattr(adapter, "call_api", fake_call_api)
    assert await adapter.is_friend("654321") is True
    assert await adapter.is_friend("111") is False
    assert calls == [("get_friend_list", {"no_cache": True})]  # 第二次走缓存
    # refresh=True 跳过缓存重查（协议端自己也缓存好友表，必须带 no_cache）
    assert await adapter.is_friend("654321", refresh=True) is True
    assert len(calls) == 2 and calls[1][1] == {"no_cache": True}

    async def broken_call_api(action, params):
        raise RuntimeError("接口崩了")

    adapter2 = OneBotAdapter(OneBotSettings(), on_message=None)  # type: ignore[arg-type]
    monkeypatch.setattr(adapter2, "call_api", broken_call_api)
    assert await adapter2.is_friend("654321") is None  # 查询失败 → 未知


async def test_adapter_fetches_doubt_friend_requests(monkeypatch):
    """可疑好友申请：主动查询 + 只能同意。"""
    adapter = OneBotAdapter(OneBotSettings(), on_message=None)  # type: ignore[arg-type]
    calls = []

    async def fake_call_api(action, params):
        calls.append((action, params))
        if action == "get_doubt_friends_add_request":
            return {
                "status": "ok",
                "data": [
                    {"user_id": 555, "nickname": "陌生人", "reason": "群里看到的",
                     "flag": "d1"},
                    {"user_id": 666, "nickname": "没凭据的", "flag": ""},
                ],
            }
        return {"status": "ok"}

    monkeypatch.setattr(adapter, "call_api", fake_call_api)
    reqs = await adapter.fetch_pending_requests()
    assert calls[0] == ("get_doubt_friends_add_request", {"count": 50})
    assert len(reqs) == 1  # 没有 flag 的没法审批，直接跳过
    assert reqs[0].kind == "friend" and reqs[0].sub_type == "doubt"
    assert reqs[0].user_id == "555" and reqs[0].comment == "群里看到的"

    # 同意走专用接口；拒绝协议端不支持，直接返回 False 且不发请求
    assert await adapter.set_request_result(reqs[0], True) is True
    assert calls[-1] == (
        "set_doubt_friends_add_request", {"flag": "d1", "approve": True}
    )
    before = len(calls)
    assert await adapter.set_request_result(reqs[0], False) is False
    assert len(calls) == before


async def test_send_like_tool_with_times():
    likes = LikeService()
    tool = build_like_tool(likes)
    session = SessionKey(channel="fake", chat_type="private", chat_id="10001")

    def ctx(channel):
        return ToolContext(
            incoming=IncomingMessage(
                session=session, user_id="10001", user_name="hhk", text="", at_me=True
            ),
            channel=channel,
            bot_name="波奇",
        )

    channel = LikeChannel(succeed_batches=99)
    assert "5 个赞" in await tool.execute('{"times": 5}', ctx(channel))
    assert channel.like_calls == [("10001", 5)]

    # 请求 20 个但限额只够 10 个 → 说明达到限额
    channel = LikeChannel(succeed_batches=1)
    out = await tool.execute('{"times": 20}', ctx(channel))
    assert "10 个赞" in out and "限额" in out
