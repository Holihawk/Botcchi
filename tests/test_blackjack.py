"""21 点：算分/BJ 判定、大厅与退券、对庄结算（胜/BJ/平/爆）、超时。"""

import pytest
from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.games.blackjack import (
    BlackjackService,
    is_blackjack,
    score,
)


def test_score_and_blackjack():
    assert score(["A", "K"]) == 21 and is_blackjack(["A", "K"])
    assert score(["A", "A", "9"]) == 21
    assert score(["A", "A"]) == 12
    assert score(["K", "Q", "5"]) == 25
    assert score(["A", "5", "9"]) == 15  # 软 15 → 硬 15
    assert score(["7", "7", "7"]) == 21 and not is_blackjack(["7", "7", "7"])


@pytest.fixture
async def ctx(mock_openai, tmp_path):
    from botcchi.app import build_context

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    context = build_context(settings, client=mock_openai.client())
    await context.db.init()
    bj = context.service.blackjack
    bj.lobby_seconds = 0
    bj.play_seconds = 0
    yield context
    await context.shutdown()


def msg(text: str, user_id: str = "10001") -> IncomingMessage:
    return IncomingMessage(
        session=SessionKey(channel="fake", chat_type="group", chat_id="777"),
        user_id=user_id,
        user_name=f"玩家{user_id[-1]}",
        text=text,
    )


async def give_tickets(ctx, *user_ids: str, n: int = 2):
    from botcchi.social.shop import add_item

    async with ctx.db.session() as s:
        for uid in user_ids:
            await add_item(s, uid, "game_ticket", n)
        await s.commit()


async def tickets(ctx, user_id: str) -> int:
    from botcchi.social.shop import item_count

    async with ctx.db.session() as s:
        return await item_count(s, user_id, "game_ticket")


async def coins(ctx, user_id: str) -> int:
    return (await ctx.service.social.profile(user_id)).coins


def rig_deck(service: BlackjackService, cards: list[str]) -> None:
    """按发牌顺序指定牌堆（pop 从末尾取，故反转）。"""
    service._new_deck = lambda: list(reversed(cards))


async def test_lobby_flow(ctx):
    channel = FakeChannel()
    bj: BlackjackService = ctx.service.blackjack
    await ctx.service.handle(msg("blackjack"), channel)
    assert "游戏券" in channel.sent[-1][1]  # 没券
    await give_tickets(ctx, "10001")
    await ctx.service.handle(msg("blackjack 梭哈"), channel)
    assert "赛制有「对庄」" in channel.sent[-1][1]
    await ctx.service.handle(msg("Blackjack"), channel)  # 大小写不敏感
    assert "开了一桌" in channel.sent[-1][1]
    # 流局退券
    await bj._lobby_timeout(bj.games["777"])
    assert await tickets(ctx, "10001") == 2


async def test_solo_win_and_dealer_rules(ctx):
    channel = FakeChannel()
    bj: BlackjackService = ctx.service.blackjack
    await give_tickets(ctx, "10001")
    base = await coins(ctx, "10001")
    # 玩家 K,10=20；庄家明 6 暗 K，补一张 2 → 18
    rig_deck(bj, ["K", "10", "6", "K", "2"])
    await ctx.service.handle(msg("blackjack"), channel)
    await ctx.service.handle(msg("开始"), channel)
    assert "[6 ?]" in channel.sent[-1][1]  # 庄家一明一暗
    await ctx.service.handle(msg("停牌"), channel)
    final = channel.sent[-1][1]
    assert "胜！+200" in final and "点数:18" in final
    assert await coins(ctx, "10001") == base + 200
    assert bj.games == {}


async def test_blackjack_premium_and_push_and_bust(ctx):
    channel = FakeChannel()
    bj: BlackjackService = ctx.service.blackjack
    users = ["10001", "10002", "10003"]
    await give_tickets(ctx, *users)
    bases = {u: await coins(ctx, u) for u in users}
    # 10001: A,K = Blackjack（自动停牌）；10002: K,9=19；10003: K,6=16
    # 庄家: 10,9=19；10003 拿牌抽 Q 爆掉
    rig_deck(bj, ["A", "K", "K", "9", "K", "6", "10", "9", "Q"])
    for u in users:
        await ctx.service.handle(msg("blackjack", user_id=u), channel)
    await ctx.service.handle(msg("开始"), channel)
    assert "Blackjack!" in channel.sent[-1][1]
    # BJ 玩家已自动停牌，重复停牌无效
    assert not await bj.handle(msg("停牌", user_id="10001"), channel, False)
    await ctx.service.handle(msg("拿牌", user_id="10003"), channel)
    assert "爆掉" in channel.sent[-1][1]
    await ctx.service.handle(msg("停牌", user_id="10002"), channel)
    final = channel.sent[-1][1]
    assert "Blackjack 胜！+250" in final
    assert "平局" in final and "爆牌" in final
    assert await coins(ctx, "10001") == bases["10001"] + 250
    assert await coins(ctx, "10002") == bases["10002"]  # 19 平 19 退券
    assert await tickets(ctx, "10002") == 2
    assert await coins(ctx, "10003") == bases["10003"]
    assert await tickets(ctx, "10003") == 0


async def test_hidden_mode_full_game(ctx):
    channel = FakeChannel()
    bj: BlackjackService = ctx.service.blackjack
    await give_tickets(ctx, "10001", "10002")
    bases = {u: await coins(ctx, u) for u in ("10001", "10002")}
    # P1: K,9=19；P2: K,6=16 拿牌抽 Q 爆
    rig_deck(bj, ["K", "9", "K", "6", "Q"])
    await ctx.service.handle(msg("blackjack 暗牌"), channel)
    assert "暗牌" in channel.sent[-1][1]
    await ctx.service.handle(msg("开始"), channel)
    assert "至少要 2 个人" in channel.sent[-1][1]
    await ctx.service.handle(msg("blackjack", user_id="10002"), channel)
    await ctx.service.handle(msg("开始"), channel)

    # 手牌走私聊，群内公告不含牌面
    privates = [t for t in channel.sent if t[0].chat_type == "private"]
    assert len(privates) == 2
    assert "[K 9]" in privates[0][1] and privates[0][0].chat_id == "10001"
    group_deal = channel.sent[-1][1]
    assert "奖池 360" in group_deal and "[" not in group_deal

    await ctx.service.handle(msg("拿牌", user_id="10002"), channel)
    # 抽牌结果私聊，群里只报张数（不显示爆牌与牌面）
    assert "抽到 Q" in [t for t in channel.sent if t[0].chat_type == "private"][-1][1]
    assert "拿了第 3 张牌，已停牌" in channel.sent[-1][1]
    assert "Q" not in channel.sent[-1][1]

    await ctx.service.handle(msg("停牌", user_id="10001"), channel)
    final = channel.sent[-1][1]
    assert "开牌" in final and "爆掉了" in final
    assert "🏆 玩家1 赢得 360 金币" in final  # 独赢全池
    assert await coins(ctx, "10001") == bases["10001"] + 360
    assert await coins(ctx, "10002") == bases["10002"]
    assert bj.games == {}


async def test_hidden_mode_tie_splits_pool(ctx):
    channel = FakeChannel()
    bj: BlackjackService = ctx.service.blackjack
    await give_tickets(ctx, "10001", "10002")
    bases = {u: await coins(ctx, u) for u in ("10001", "10002")}
    rig_deck(bj, ["10", "10", "J", "Q"])  # 双方都是 20
    await ctx.service.handle(msg("blackjack 暗牌"), channel)
    await ctx.service.handle(msg("blackjack", user_id="10002"), channel)
    await ctx.service.handle(msg("开始"), channel)
    await ctx.service.handle(msg("停牌", user_id="10001"), channel)
    await ctx.service.handle(msg("停牌", user_id="10002"), channel)
    assert await coins(ctx, "10001") == bases["10001"] + 180
    assert await coins(ctx, "10002") == bases["10002"] + 180


async def test_hidden_mode_private_failure_aborts(ctx):
    channel = FakeChannel()
    bj: BlackjackService = ctx.service.blackjack

    class NoPrivateChannel(FakeChannel):
        async def send_text(self, session, text, *, reply_to=None, at_user=None):
            if session.chat_type == "private":
                raise RuntimeError("not friend")
            await super().send_text(session, text, at_user=at_user)

    blocked_channel = NoPrivateChannel()
    await give_tickets(ctx, "10001", "10002")
    rig_deck(bj, ["K", "9", "K", "6"])
    await ctx.service.handle(msg("blackjack 暗牌"), blocked_channel)
    await ctx.service.handle(msg("blackjack", user_id="10002"), blocked_channel)
    await ctx.service.handle(msg("开始"), blocked_channel)
    assert "流局" in blocked_channel.sent[-1][1]
    assert bj.games == {}
    assert await tickets(ctx, "10001") == 2
    assert await tickets(ctx, "10002") == 2
    _ = channel


async def test_unknown_mode_hint(ctx):
    channel = FakeChannel()
    await give_tickets(ctx, "10001")
    await ctx.service.handle(msg("blackjack 锦标赛"), channel)
    assert "赛制有「对庄」" in channel.sent[-1][1]
    assert ctx.service.blackjack.games == {}


async def test_play_timeout_auto_stands(ctx):
    channel = FakeChannel()
    bj: BlackjackService = ctx.service.blackjack
    await give_tickets(ctx, "10001")
    # 玩家 5,6=11；庄家 K,Q=20 → 超时自动停牌判负
    rig_deck(bj, ["5", "6", "K", "Q"])
    await ctx.service.handle(msg("blackjack"), channel)
    await ctx.service.handle(msg("开始"), channel)
    await bj._play_timeout(bj.games["777"])
    final = channel.sent[-1][1]
    assert "超时自动停牌" in final and "负" in final
    assert bj.games == {}
