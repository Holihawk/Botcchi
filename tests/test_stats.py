"""调用统计与崩溃恢复：记录点、聚合接口、游戏券托管退款。"""

import pytest
from sqlalchemy import select
from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.db.models import GameEscrowRow, UsageStatRow


@pytest.fixture
async def ctx(mock_openai, tmp_path):
    from botcchi.app import build_context

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    context = build_context(settings, client=mock_openai.client())
    await context.db.init()
    if context.service.duel is not None:
        context.service.duel.challenge_seconds = 0
    yield context
    await context.shutdown()


def msg(text: str, user_id: str = "10001", **kw) -> IncomingMessage:
    return IncomingMessage(
        session=SessionKey(channel="fake", chat_type="group", chat_id="777"),
        user_id=user_id,
        user_name="hhk",
        text=text,
        **kw,
    )


async def stat_rows(ctx) -> list[UsageStatRow]:
    async with ctx.db.session() as s:
        return (await s.scalars(select(UsageStatRow))).all()


async def test_command_and_chat_recorded(ctx, mock_openai):
    from conftest import text_chunks

    channel = FakeChannel()
    await ctx.service.handle(msg("签到"), channel)
    await ctx.service.handle(msg("我的背包"), channel)
    rows = await stat_rows(ctx)
    assert [(r.kind, r.name) for r in rows] == [
        ("command", "签到"), ("command", "商店"),
    ]
    assert rows[0].user_id == "10001" and rows[0].group_id == "777"
    assert len(rows[0].day) == 10

    # AI 对话（@必回）记 chat/闲聊
    mock_openai.enqueue(text_chunks("在的在的！"))
    await ctx.service.handle(msg("波奇在吗", at_me=True), channel)
    rows = await stat_rows(ctx)
    assert ("chat", "闲聊") in [(r.kind, r.name) for r in rows]

    # 未命中任何功能的指令（帮助）不计
    await ctx.service.handle(msg("帮助"), channel)
    assert len(await stat_rows(ctx)) == 3


async def test_tool_call_recorded(ctx):
    from botcchi.agent.messages import ToolCall
    from botcchi.tools.registry import ToolContext

    tool_ctx = ToolContext(
        incoming=msg(""), channel=FakeChannel(), stats=ctx.service.stats
    )
    out = await ctx.service.loop.tools.execute(
        ToolCall(id="1", name="current_time", arguments="{}"), tool_ctx
    )
    assert "星期" in out
    rows = await stat_rows(ctx)
    assert [(r.kind, r.name) for r in rows] == [("tool", "current_time")]


async def test_overview(ctx):
    stats = ctx.service.stats
    for _ in range(3):
        await stats.add("command", "签到", user_id="10001", user_name="hhk",
                        group_id="777")
    await stats.add("tool", "get_weather", user_id="10002", user_name="阿波")
    data = await stats.overview(days=7)
    assert data["total"] == 4 and data["today"] == 4
    assert len(data["daily"]) == 7 and data["daily"][-1]["count"] == 4
    assert data["by_name"][0] == {"kind": "command", "name": "签到", "count": 3}
    assert data["top_users"][0]["user_id"] == "10001"
    assert data["top_groups"] == [{"group_id": "777", "count": 3}]


async def test_escrow_lifecycle_and_crash_refund(ctx):
    """决斗入座记托管；正常终局清托管；崩溃残留由启动退款兜底。"""
    from botcchi.social.shop import add_item, item_count, refund_escrows

    async with ctx.db.session() as s:
        await add_item(s, "10001", "game_ticket", 3)
        await s.commit()
    channel = FakeChannel()
    await ctx.service.handle(msg("决斗", mentions=["10002"]), channel)

    async with ctx.db.session() as s:
        rows = (await s.scalars(select(GameEscrowRow))).all()
        assert [(r.game, r.user_id, r.tickets) for r in rows] == [
            ("duel", "10001", 3)
        ]
        assert await item_count(s, "10001", "game_ticket") == 0

    # 模拟进程崩溃重启：内存对局丢失，启动时托管退款
    ctx.service.duel.games.clear()
    users, tickets = await refund_escrows(ctx.db)
    assert (users, tickets) == (1, 3)
    async with ctx.db.session() as s:
        assert await item_count(s, "10001", "game_ticket") == 3
        assert (await s.scalars(select(GameEscrowRow))).all() == []
    # 幂等：再退一次无事发生
    assert await refund_escrows(ctx.db) == (0, 0)


async def test_escrow_cleared_on_normal_end(ctx):
    """对方拒绝 → 对局终结（退款照常），托管表出清，重启不会重复退款。"""
    from botcchi.social.shop import add_item, item_count, refund_escrows

    async with ctx.db.session() as s:
        await add_item(s, "10001", "game_ticket", 3)
        await s.commit()
    channel = FakeChannel()
    await ctx.service.handle(msg("决斗", mentions=["10002"]), channel)
    await ctx.service.handle(msg("拒绝", user_id="10002"), channel)
    assert "婉拒" in channel.sent[-1][1]

    async with ctx.db.session() as s:
        assert (await s.scalars(select(GameEscrowRow))).all() == []
        assert await item_count(s, "10001", "game_ticket") == 3
    assert await refund_escrows(ctx.db) == (0, 0)
