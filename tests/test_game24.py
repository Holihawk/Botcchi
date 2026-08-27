"""24 点：求解器/表达式解析/规范化、大厅、抢答、路人烧解法、奖金顺延。"""

from fractions import Fraction

import pytest
from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.games.game24 import (
    Game24Service,
    looks_like_expr,
    parse_expr,
    solve24,
)


def test_solver():
    assert solve24([1, 1, 1, 1]) is None
    for cards in ([3, 3, 8, 8], [1, 5, 5, 5], [4, 6, 1, 1], [13, 13, 12, 1]):
        expr = solve24(cards)
        assert expr is not None
        value, nums, _ = parse_expr(expr.replace("×", "*").replace("÷", "/"))
        assert value == 24 and sorted(nums) == sorted(cards)


def test_parse_and_canonical():
    # 分数运算：经典 8/(3-8/3)
    value, nums, _ = parse_expr("8/(3-8/3)")
    assert value == 24 and sorted(nums) == [3, 3, 8, 8]
    assert parse_expr("3 + ") is None
    assert parse_expr("__import__('os')") is None
    value, _, _ = parse_expr("1/3")
    assert value == Fraction(1, 3)
    # 交换律/结合律 → 同一个规范化键
    k1 = parse_expr("(3+5)*(1+2)")[2]
    k2 = parse_expr("(2+1)×(5+3)")[2]
    assert k1 == k2
    k3 = parse_expr("2*3*4")[2]
    k4 = parse_expr("4*(3*2)")[2]
    assert k3 == k4
    # 不同解法 → 不同键
    assert parse_expr("8/(3-8/3)")[2] != parse_expr("3*8*(3-8)")[2]

    assert looks_like_expr("(3+5)×(1+2)")
    assert looks_like_expr("8/(3-8/3)")
    assert not looks_like_expr("你好呀")
    assert not looks_like_expr("3388")  # 没有运算符


@pytest.fixture
async def ctx(mock_openai, tmp_path):
    from botcchi.app import build_context

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    context = build_context(settings, client=mock_openai.client())
    await context.db.init()
    g24 = context.service.game24
    g24.lobby_seconds = 0
    g24.round_seconds = 0
    yield context
    await context.shutdown()


def msg(text: str, user_id: str = "10001") -> IncomingMessage:
    return IncomingMessage(
        session=SessionKey(channel="fake", chat_type="group", chat_id="777"),
        user_id=user_id,
        user_name=f"玩家{user_id[-1]}",
        text=text,
    )


async def give_tickets(ctx, *user_ids: str, n: int = 1):
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


async def test_lobby_and_refund(ctx):
    channel = FakeChannel()
    g24: Game24Service = ctx.service.game24
    await ctx.service.handle(msg("24点"), channel)
    assert "游戏券" in channel.sent[-1][1]  # 没券
    await give_tickets(ctx, "10001", "10002")
    await ctx.service.handle(msg("24点"), channel)
    assert "开了一桌" in channel.sent[-1][1]
    await ctx.service.handle(msg("开始"), channel)
    assert "至少要 2 个人" in channel.sent[-1][1]
    await ctx.service.handle(msg("24点", user_id="10002"), channel)
    assert "入座" in channel.sent[-1][1]
    # 流局退券
    await g24._lobby_timeout(g24.games["777"])
    assert "流局" in channel.sent[-1][1]
    assert await tickets(ctx, "10001") == 1
    assert await tickets(ctx, "10002") == 1


async def test_full_game_with_burn_and_carry(ctx):
    channel = FakeChannel()
    g24: Game24Service = ctx.service.game24
    g24.rounds = 2
    await give_tickets(ctx, "10001", "10002")
    base1 = await coins(ctx, "10001")

    await ctx.service.handle(msg("24点"), channel)
    await ctx.service.handle(msg("24点", user_id="10002"), channel)
    await ctx.service.handle(msg("开始"), channel)
    game = g24.games["777"]
    assert game.phase == "playing" and game.round_no == 1
    assert game.per_round == 80  # 奖池 160 / 2 轮

    # 固定牌面
    game.cards = [3, 3, 8, 8]
    # 普通聊天不受影响
    assert not await g24.handle(msg("大家好啊", user_id="10001"), channel, False)
    # 路人喊出正确答案 → 烧解法
    await ctx.service.handle(msg("8/(3-8/3)", user_id="90009"), channel)
    assert "作废" in channel.sent[-1][1]
    # 玩家提交同一解法（换种写法）→ 不算、不扣次数
    await ctx.service.handle(msg("8÷(3-8÷3)", user_id="10001"), channel)
    assert "换一个" in channel.sent[-1][1]
    assert game.attempts.get("10001", 0) == 0
    # 错误答案扣次数
    await ctx.service.handle(msg("3+3+8+8", user_id="10001"), channel)
    assert "不是 24" in channel.sent[-1][1] and "还剩 2 次" in channel.sent[-1][1]
    # 数字不符扣次数
    await ctx.service.handle(msg("3*8", user_id="10001"), channel)
    assert "4 个数字" in channel.sent[-1][1]
    # 3,3,8,8 只有一种解法，被烧掉后玩家们只能用完次数
    await ctx.service.handle(msg("8*(3-3)+8", user_id="10001"), channel)
    assert game.attempts["10001"] == 3
    await ctx.service.handle(msg("3+3+8+8", user_id="10001"), channel)
    assert "用完" in channel.sent[-1][1]
    for i in range(3):
        await ctx.service.handle(msg("3-3+8-8", user_id="10002"), channel)
    # 全员用完 → 公布解、奖金顺延、进入第 2 轮
    joined = "".join(t[1] for t in channel.sent[-3:])
    assert "无人解出" in joined and "顺延" in joined
    assert game.round_no == 2 and game.carry == 80

    # 第 2 轮：含顺延奖金 160
    assert "含顺延奖金" in channel.sent[-1][1]
    game.cards = [1, 2, 3, 4]
    await ctx.service.handle(msg("1*2*3*4", user_id="10001"), channel)
    assert "+160 金币" in channel.sent[-2][1]
    # 收官战报
    assert "战报" in channel.sent[-1][1]
    assert await coins(ctx, "10001") == base1 + 160
    assert g24.games == {}


async def test_round_timeout_reveals_solution(ctx):
    channel = FakeChannel()
    g24: Game24Service = ctx.service.game24
    g24.rounds = 2
    await give_tickets(ctx, "10001", "10002")
    await ctx.service.handle(msg("24点"), channel)
    await ctx.service.handle(msg("24点", user_id="10002"), channel)
    await ctx.service.handle(msg("开始"), channel)
    game = g24.games["777"]
    game.cards = [3, 3, 8, 8]
    await g24._round_timeout(game)
    joined = "".join(t[1] for t in channel.sent[-2:])
    assert "时间到" in joined and "参考答案" in joined and "= 24" in joined
    assert game.round_no == 2
    await g24.aclose()
