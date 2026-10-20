"""商店：购买/佩戴/背包、双倍卡消耗、挂件渲染、商店卡片与金币工具调用费。"""

from datetime import timedelta

import pytest
from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.config import RenderSettings, SocialSettings
from botcchi.db.engine import Database
from botcchi.render import SvgRenderer
from botcchi.social.card import CardData, build_card
from botcchi.social.service import SocialProfile, SocialService, today
from botcchi.social.shop import GOODS, ShelfEntry, ShopService, ToolCharger
from botcchi.social.shop_card import build_shop_card
from botcchi.tools.registry import ToolContext, ToolRegistry, tool


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/shop.db")
    await database.init()
    yield database
    await database.aclose()


@pytest.fixture
def social(db):
    # 初始金币写死：本文件多处依赖「买得起游戏券、买不起发夹」这条线
    return SocialService(
        SocialSettings(double_probability=0.0, initial_coins=200), db
    )


@pytest.fixture
def shop(db, social):
    return ShopService(db, social)


async def test_buy_flow(social, shop):
    # 初始 200 金币：买得起游戏券，买不起发夹
    reply = await shop.buy("10001", "hhk", "神秘发夹-粉")
    assert "金币不够" in reply and "200 金币" in reply
    # 未知商品给出货架提示
    assert "商店里没有" in await shop.buy("10001", "hhk", "谜之商品")
    assert "购买「游戏券」成功" in await shop.buy("10001", "hhk", "游戏券")

    await social.add_coins("10001", 20000, "test")
    reply = await shop.buy("10001", "hhk", "神秘发夹-粉")
    assert "购买「神秘发夹-粉」成功" in reply
    assert "自动帮你戴上" in reply  # 首个挂件自动佩戴
    assert "好感度 +" in reply  # 首购好感度赠礼
    p = await social.profile("10001")
    assert p.pendant == "hairpin_pink"
    assert 8.0 <= p.impression <= 10.0
    assert p.coins == 200 - 50 + 20000 - 9999
    # 限购一次
    assert "限购一次" in await shop.buy("10001", "hhk", "神秘发夹-粉")

    # 消耗品可以重复买、可带数量
    assert "成功" in await shop.buy("10001", "hhk", "好感度双倍加持卡")
    assert "×2」成功！花费 600" in await shop.buy(
        "10001", "hhk", "好感度双倍加持卡", 2
    )
    assert "限购一次" in await shop.buy("10001", "hhk", "神秘发夹-粉", 2)
    assert "最多买 99" in await shop.buy("10001", "hhk", "游戏券", 100)
    entries, coins = await shop.shelf("10001")
    by_id = {e.goods.id: e for e in entries}
    assert by_id["double_card"].owned == 3
    assert by_id["hairpin_pink"].equipped
    assert coins == 200 - 50 + 20000 - 9999 - 900

    text = await shop.inventory_text("10001")
    assert "好感度双倍加持卡 ×3" in text and "佩戴中" in text


async def test_equip_unequip(social, shop):
    assert "先在商店购买" in await shop.equip("10001", "神秘发夹-粉")
    assert "没有叫" in await shop.equip("10001", "好感度双倍加持卡")
    assert "没有佩戴挂件" in await shop.unequip("10001")

    await social.add_coins("10001", 10000, "test")
    await shop.buy("10001", "hhk", "神秘发夹-粉")
    assert "取下「神秘发夹-粉」" in await shop.unequip("10001")
    assert (await social.profile("10001")).pendant is None
    assert "戴上「神秘发夹-粉」" in await shop.equip("10001", "神秘发夹-粉")
    assert (await social.profile("10001")).pendant == "hairpin_pink"


async def test_double_card_consumed_on_sign(social, shop, db):
    from sqlalchemy import update

    from botcchi.db.models import SocialUserRow

    await social.add_coins("10001", 1000, "test")
    await shop.buy("10001", "hhk", "好感度双倍加持卡")
    await shop.buy("10001", "hhk", "好感度双倍加持卡")

    result = await social.sign("10001", "hhk")  # double_probability=0 → 必来自卡
    assert result.double and result.double_card

    async with db.session() as s:  # 回拨到昨天再签一次
        await s.execute(
            update(SocialUserRow)
            .where(SocialUserRow.user_id == "10001")
            .values(last_sign_date=(today() - timedelta(days=1)).isoformat())
        )
        await s.commit()
    result = await social.sign("10001", "hhk")
    assert result.double_card
    entries, _ = await shop.shelf("10001")
    assert {e.goods.id: e.owned for e in entries}["double_card"] == 0


def test_pendant_in_sign_card():
    profile = SocialProfile(
        user_id="10001", user_name="hhk", impression=10, coins=100,
        sign_count=3, streak=3, last_sign_date="2026-07-19",
        pendant="hairpin_pink",
    )
    data = CardData(profile=profile, mode="profile", message="hi", theme="pink")
    svg = build_card(data)
    assert 'rotate(20' in svg  # 发夹沿用旧版的 20° 倾斜贴合
    profile.pendant = None
    assert 'rotate(20' not in build_card(data)

    data.double = data.double_card = True
    data.mode = "sign"
    assert "双倍卡!" in build_card(data)


async def test_shop_card_render():
    entries = [
        ShelfEntry(GOODS["hairpin_pink"], owned=1, equipped=True),
        ShelfEntry(GOODS["double_card"], owned=2, equipped=False),
        ShelfEntry(GOODS["game_ticket"], owned=0, equipped=False),
    ]
    svg = build_shop_card(entries)
    assert "波奇の商店" in svg
    assert "金币 " not in svg.replace("9999 金币", "").replace(
        "300 金币", ""
    ).replace("50 金币", "")  # 不含余额（保证缓存命中）
    assert "神秘发夹-粉" in svg and "9999 金币" in svg and "游戏券" in svg
    assert "佩戴中" in svg and "已拥有 ×2" in svg
    png = SvgRenderer(RenderSettings()).to_png(svg)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


async def test_render_cache(tmp_path):
    from botcchi.render import RenderCache

    renderer = SvgRenderer(RenderSettings())
    cache = RenderCache(tmp_path / "cards", max_files=2)
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'

    calls = 0
    real = renderer.render

    async def counting(s, **kw):
        nonlocal calls
        calls += 1
        return await real(s, **kw)

    renderer.render = counting
    png1 = await cache.render(renderer, svg)
    png2 = await cache.render(renderer, svg)  # 内存命中
    assert png1 == png2 and calls == 1
    assert len(list((tmp_path / "cards").glob("*.png"))) == 1

    cache2 = RenderCache(tmp_path / "cards")  # 新进程 → 磁盘命中
    assert await cache2.render(renderer, svg) == png1 and calls == 1

    # 内容变化 → 重渲染
    await cache.render(renderer, svg.replace("10", "20"))
    assert calls == 2


async def test_shop_tools(social, shop):
    from botcchi.social.shop import build_shop_tools
    from botcchi.tools.registry import ToolContext

    equip_tool, backpack_tool = build_shop_tools(shop)
    ctx = ToolContext(incoming=_msg("10001"), channel=FakeChannel())

    out = await backpack_tool.execute("{}", ctx)
    assert "背包空空" in out
    await social.add_coins("10001", 10000, "test")
    await shop.buy("10001", "hhk", "神秘发夹-粉")

    out = await equip_tool.execute('{"remove": true}', ctx)
    assert "取下「神秘发夹-粉」" in out
    out = await equip_tool.execute('{"name": "神秘发夹-粉"}', ctx)
    assert "戴上「神秘发夹-粉」" in out
    out = await backpack_tool.execute("{}", ctx)
    assert "神秘发夹-粉 ×1" in out and "佩戴中" in out


def _msg(user_id="10001") -> IncomingMessage:
    return IncomingMessage(
        session=SessionKey(channel="fake", chat_type="group", chat_id="777"),
        user_id=user_id,
        user_name="hhk",
        text="",
        at_me=True,
    )


async def test_tool_charger(social):
    registry = ToolRegistry()

    calls = []

    @tool(description="echo")
    async def paid_echo(text: str) -> str:
        calls.append(text)
        return f"echo: {text}"

    registry.register(paid_echo)
    charger = ToolCharger(
        social, {"paid_echo": 300}, is_superuser=lambda uid: uid == "900000"
    )
    channel = FakeChannel()

    def ctx(user_id):
        return ToolContext(
            incoming=_msg(user_id), channel=channel, charger=charger
        )

    from botcchi.agent.messages import ToolCall

    call = ToolCall(id="1", name="paid_echo", arguments='{"text": "hi"}')
    # 初始 200 < 300：拒绝且不执行
    out = await registry.execute(call, ctx("10001"))
    assert "金币不足" in out and calls == []
    # 充到 400：扣 300
    await social.add_coins("10001", 200, "test")
    out = await registry.execute(call, ctx("10001"))
    assert out == "echo: hi"
    assert (await social.profile("10001")).coins == 100
    # 超管免费（不扣初始金币）
    out = await registry.execute(call, ctx("900000"))
    assert out == "echo: hi"
    p = await social.peek("900000")
    assert p is None or p.coins == social.settings.initial_coins

    # 工具抛异常 → 退款
    @tool(description="boom")
    async def paid_boom() -> str:
        raise RuntimeError("boom")

    registry.register(paid_boom)
    charger.costs["paid_boom"] = 50
    out = await registry.execute(
        ToolCall(id="2", name="paid_boom", arguments="{}"), ctx("10001")
    )
    assert "工具执行失败" in out
    assert (await social.profile("10001")).coins == 100  # 已退款


async def test_shop_commands_integration(mock_openai, tmp_path):
    from botcchi.app import build_context

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    settings.onebot.superusers = ["900000"]
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()

    class ImageChannel(FakeChannel):
        def __init__(self):
            super().__init__()
            self.images = []

        async def send_image(self, session, file):
            self.images.append(file)

    channel = ImageChannel()

    def msg(text, user_id="10001"):
        m = _msg(user_id)
        m.text = text
        return m

    await ctx.service.handle(msg("商店"), channel)
    assert len(channel.images) == 1  # 商店卡片
    assert channel.images[0].startswith("base64://")  # OneBot file 格式

    await ctx.service.handle(msg("购买"), channel)
    assert "用法" in channel.sent[-1][1]
    await ctx.service.handle(msg("购买 神秘发夹-粉"), channel)
    assert "金币不够" in channel.sent[-1][1]  # 初始金币远不够 9999

    await ctx.service.handle(msg("购买 游戏券 3"), channel)  # 初始 200 够 150
    assert "游戏券×3」成功" in channel.sent[-1][1]
    await ctx.service.handle(msg("购买\u3000游戏券", user_id="10002"), channel)
    assert "购买「游戏券」成功" in channel.sent[-1][1]
    await ctx.service.handle(msg("我的背包"), channel)
    assert "游戏券 ×3" in channel.sent[-1][1]

    # 功能开关：关闭商店后指令被拦截
    await ctx.service.handle(msg("关闭功能 商店", user_id="900000"), channel)
    await ctx.service.handle(msg("商店"), channel)
    assert "「商店」在本群已被关闭" in channel.sent[-1][1]
    assert len(channel.images) == 1
    await ctx.shutdown()
