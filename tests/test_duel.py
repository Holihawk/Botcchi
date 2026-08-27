"""决斗小游戏：约战/大厅/彩蛋、游戏券消耗与退还、对局机制、奖池结算。"""

import pytest
from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.games.duel import DuelService


@pytest.fixture
async def ctx(mock_openai, tmp_path):
    from botcchi.app import build_context

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    context = build_context(settings, client=mock_openai.client())
    await context.db.init()
    # 测试关闭全部定时器（超时路径手动触发）
    duel = context.service.duel
    duel.challenge_seconds = 0
    duel.lobby_seconds = 0
    duel.choose_seconds = 0
    duel.turn_seconds = 0
    yield context
    await context.shutdown()


def msg(
    text: str, user_id: str = "10001", name: str | None = None,
    mentions: list[str] | None = None, at_me: bool = False,
) -> IncomingMessage:
    return IncomingMessage(
        session=SessionKey(channel="fake", chat_type="group", chat_id="777"),
        user_id=user_id,
        user_name=name or f"玩家{user_id[-1]}",
        text=text,
        at_me=at_me,
        mentions=mentions or [],
        self_id="99999",
    )


async def tickets(ctx, user_id: str) -> int:
    from botcchi.social.shop import item_count

    async with ctx.db.session() as s:
        return await item_count(s, user_id, "game_ticket")


async def coins(ctx, user_id: str) -> int:
    # 用 profile 建档（含 200 初始金币），基线与结算后可直接做差
    return (await ctx.service.social.profile(user_id)).coins


async def give_tickets(ctx, *user_ids: str, n: int = 3):
    from botcchi.social.shop import add_item

    async with ctx.db.session() as s:
        for uid in user_ids:
            await add_item(s, uid, "game_ticket", n)
        await s.commit()


async def test_easter_egg_and_no_tickets(ctx):
    channel = FakeChannel()
    # 约 bot：彩蛋
    await ctx.service.handle(msg("决斗", at_me=True), channel)
    assert "不敢玩这么危险的游戏" in channel.sent[-1][1]
    # 没券开不了桌
    await ctx.service.handle(msg("决斗"), channel)
    assert "游戏券" in channel.sent[-1][1] and "购买" in channel.sent[-1][1]
    assert ctx.service.duel.games == {}


async def test_challenge_refuse_refunds(ctx):
    channel = FakeChannel()
    await give_tickets(ctx, "10001")
    await ctx.service.handle(msg("决斗", mentions=["10002"]), channel)
    assert await tickets(ctx, "10001") == 0  # 已消耗
    await ctx.service.handle(msg("拒绝", user_id="10002"), channel)
    assert "婉拒" in channel.sent[-1][1]
    assert await tickets(ctx, "10001") == 3  # 已退还
    assert ctx.service.duel.games == {}


async def test_choose_item_accepts_loose_wording(ctx):
    """选道具放宽：「选择 X」「我选 X」「X」都算，含糊时才追问。"""
    channel = FakeChannel()
    duel: DuelService = ctx.service.duel
    await give_tickets(ctx, "10001", "10002")
    await ctx.service.handle(msg("决斗", mentions=["10002"]), channel)
    await ctx.service.handle(msg("接受", user_id="10002"), channel)
    game = duel.games["777"]

    # 同时提到两个道具：不猜，追问
    await ctx.service.handle(msg("选择手铐还是透镜呢", user_id="10002"), channel)
    assert "一次只能选一个" in channel.sent[-1][1]
    assert not game.players[0].chosen

    await ctx.service.handle(msg("选择透镜", user_id="10002"), channel)
    assert game.players[0].items == ["透镜"]
    # 光说道具名也能选上（choosing 阶段、且只提到一个）
    await ctx.service.handle(msg("手铐", user_id="10001"), channel)
    assert game.player("10001").items == ["手铐"]
    assert game.phase == "playing"


async def test_1v1_full_game(ctx):
    channel = FakeChannel()
    duel: DuelService = ctx.service.duel
    await give_tickets(ctx, "10001", "10002")
    base1, base2 = await coins(ctx, "10001"), await coins(ctx, "10002")

    await ctx.service.handle(msg("决斗", mentions=["10002"]), channel)
    await ctx.service.handle(msg("接受", user_id="10002"), channel)
    game = duel.games["777"]
    assert game.one_vs_one
    assert game.players[0].user_id == "10002"  # 接受者先手
    assert [p.hp for p in game.players] == [1, 1]

    # 双方选道具（1v1 只有手铐/透镜）
    await ctx.service.handle(msg("选 糖", user_id="10002"), channel)
    assert "可选" in channel.sent[-1][1]  # 1v1 没有糖
    await ctx.service.handle(msg("选 透镜", user_id="10002"), channel)
    await ctx.service.handle(msg("选 手铐", user_id="10001"), channel)
    assert game.phase == "playing"
    assert "实弹" in "".join(t[1] for t in channel.sent[-3:])  # 开局公告

    # 固定弹匣：空、实
    game.magazine = [False, True]
    # 非当前玩家开枪无效
    assert not await duel.handle(msg("打对面", user_id="10001"), channel, False)
    # 接受者赌空弹打自己 → 继续行动
    await ctx.service.handle(msg("打自己", user_id="10002"), channel)
    assert "空弹" in channel.sent[-2][1] and "继续行动" in channel.sent[-2][1]
    assert game.current.user_id == "10002"
    # 实弹打对面 → 结束
    await ctx.service.handle(msg("打对面", user_id="10002"), channel)
    final = channel.sent[-1][1]
    assert "出局" in "".join(t[1] for t in channel.sent[-2:])
    assert "赢家" in final and "+240" in final  # 1v1 全拿 120×2
    assert await coins(ctx, "10002") == base2 + 240
    assert await coins(ctx, "10001") == base1
    assert duel.games == {}


async def test_multi_game_payouts_and_items(ctx):
    channel = FakeChannel()
    duel: DuelService = ctx.service.duel
    users = ["10001", "10002", "10003"]
    await give_tickets(ctx, *users)
    bases = {u: await coins(ctx, u) for u in users}

    await ctx.service.handle(msg("决斗", user_id="10001"), channel)
    await ctx.service.handle(msg("决斗", user_id="10002"), channel)
    await ctx.service.handle(msg("决斗", user_id="10002"), channel)
    assert "已经在桌上" in channel.sent[-1][1]
    await ctx.service.handle(msg("决斗", user_id="10003"), channel)
    # 只有发起者能开局
    assert not await duel.handle(msg("开始", user_id="10002"), channel, False)
    await ctx.service.handle(msg("开始", user_id="10001"), channel)
    game = duel.games["777"]
    assert not game.one_vs_one
    assert [p.hp for p in game.players] == [2, 2, 2]
    assert game.players[0].user_id == "10001"  # 发起者先手

    for uid, item in (("10001", "延长管"), ("10002", "糖"), ("10003", "手铐")):
        await ctx.service.handle(msg(f"选 {item}", user_id=uid), channel)
    assert game.phase == "playing"

    game.magazine = [True, False, True, True]
    # 发起者装延长管，一枪双倍带走 10002
    await ctx.service.handle(msg("用 延长管", user_id="10001"), channel)
    assert "翻倍" in channel.sent[-1][1]
    await ctx.service.handle(
        msg("打 @玩家2", user_id="10001", mentions=["10002"]), channel
    )
    assert "出局" in channel.sent[-2][1]
    # 10003 铐住 10001 → 10001 回合被跳过，又轮回 10003
    assert game.current.user_id == "10003"
    await ctx.service.handle(
        msg("用 手铐 @玩家1", user_id="10003", mentions=["10001"]), channel
    )
    await ctx.service.handle(msg("打自己", user_id="10003"), channel)  # 空弹继续
    assert game.current.user_id == "10003"
    await ctx.service.handle(msg("打自己", user_id="10003"), channel)  # 实弹 -1
    assert game.player("10003").hp == 1
    assert game.current.user_id == "10003"  # 10001 被铐跳过
    game.magazine = [True]
    await ctx.service.handle(
        msg("打 @玩家1", user_id="10003", mentions=["10001"]), channel
    )
    assert game.player("10001").hp == 1
    # 弹匣打空自动换弹并补发道具
    joined = "".join(t[1] for t in channel.sent[-4:])
    assert "换弹" in joined and "获得道具" in joined
    game.magazine = [True, True]
    assert game.current.user_id == "10001"
    await ctx.service.handle(
        msg("打 @玩家3", user_id="10001", mentions=["10003"]), channel
    )
    # 10003 出局 → 10001 胜。奖池 360：冠军 252 / 亚军 72 / 季军 36
    final = channel.sent[-1][1]
    assert "赢家" in final
    assert await coins(ctx, "10001") == bases["10001"] + 252
    assert await coins(ctx, "10003") == bases["10003"] + 72  # 后出局是亚军
    assert await coins(ctx, "10002") == bases["10002"] + 36
    assert duel.games == {}


async def test_two_player_lobby_becomes_1v1(ctx):
    channel = FakeChannel()
    duel: DuelService = ctx.service.duel
    await give_tickets(ctx, "10001", "10002")
    await ctx.service.handle(msg("决斗", user_id="10001"), channel)
    await ctx.service.handle(msg("决斗", user_id="10002"), channel)
    await ctx.service.handle(msg("开始", user_id="10001"), channel)
    game = duel.games["777"]
    assert game.one_vs_one and [p.hp for p in game.players] == [1, 1]
    assert game.players[0].user_id == "10002"  # 后入座者先手
    # 流局路径：手动触发大厅超时不再适用（已开局），改为直接清场
    await duel.aclose()


async def test_lobby_timeout_refunds_all(ctx):
    channel = FakeChannel()
    duel: DuelService = ctx.service.duel
    await give_tickets(ctx, "10001", "10002")
    await ctx.service.handle(msg("决斗", user_id="10001"), channel)
    await ctx.service.handle(msg("决斗", user_id="10002"), channel)
    await duel._lobby_timeout(duel.games["777"])
    assert "流局" in channel.sent[-1][1]
    assert await tickets(ctx, "10001") == 3
    assert await tickets(ctx, "10002") == 3


async def test_turn_timeout_forfeits_and_excludes_payout(ctx):
    channel = FakeChannel()
    duel: DuelService = ctx.service.duel
    users = ["10001", "10002", "10003"]
    await give_tickets(ctx, *users)
    bases = {u: await coins(ctx, u) for u in users}
    for u in users:
        await ctx.service.handle(msg("决斗", user_id=u), channel)
    await ctx.service.handle(msg("开始", user_id="10001"), channel)
    game = duel.games["777"]
    for u in users:
        await ctx.service.handle(msg("选 透镜", user_id=u), channel)
    # 10001 发呆判负（弃权，不参与分配）
    await duel._turn_timeout(game)
    assert game.player("10001").forfeited
    game.magazine = [True, True]
    assert game.current.user_id == "10002"
    await ctx.service.handle(
        msg("打 @玩家3", user_id="10002", mentions=["10003"]), channel
    )
    assert game.current.user_id == "10003"
    await ctx.service.handle(msg("打自己", user_id="10003"), channel)  # 实弹自尽
    # 10002 冠军：252 + 弃权者(季军 36)份额 = 288；10003 亚军 72
    assert await coins(ctx, "10002") == bases["10002"] + 252 + 36
    assert await coins(ctx, "10003") == bases["10003"] + 72
    assert await coins(ctx, "10001") == bases["10001"]


async def test_lens_private_message(ctx):
    channel = FakeChannel()
    duel: DuelService = ctx.service.duel
    await give_tickets(ctx, "10001", "10002")
    await ctx.service.handle(msg("决斗", mentions=["10002"]), channel)
    await ctx.service.handle(msg("接受", user_id="10002"), channel)
    game = duel.games["777"]
    await ctx.service.handle(msg("选 透镜", user_id="10002"), channel)
    await ctx.service.handle(msg("选 透镜", user_id="10001"), channel)
    game.magazine = [True, False]
    await ctx.service.handle(msg("用 透镜", user_id="10002"), channel)
    # 结果走私聊，群里只有"看了一眼"
    private = [t[0] for t in channel.sent if t[0].chat_type == "private"]
    assert private and private[-1].chat_id == "10002"
    assert "实弹" in [t[1] for t in channel.sent if t[0].chat_type == "private"][-1]
    assert "看了一眼" in channel.sent[-1][1]
    assert "透镜" not in game.players[0].items  # 已消耗
    await duel.aclose()
