"""Social 模块：好感度等级、签到数值、金币账本、卡片渲染与指令接入。"""

from datetime import timedelta

import pytest
from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.config import SocialSettings
from botcchi.db.engine import Database
from botcchi.render import SvgRenderer, esc, to_onebot_file
from botcchi.social.card import CardData, build_card
from botcchi.social.levels import favor_level
from botcchi.social.service import SocialService, today
from botcchi.social.tools import build_social_tools
from botcchi.tools.registry import ToolContext, ToolRegistry


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/social.db")
    await database.init()
    yield database
    await database.aclose()


@pytest.fixture
def social(db):
    # 初始金币写死，避免经济数值调整时测试断言跟着漂
    return SocialService(SocialSettings(initial_coins=200), db)


def test_favor_levels():
    assert favor_level(0).relation == "路人"
    assert favor_level(4.9).level == 0
    assert favor_level(5).relation == "陌生"
    assert favor_level(45).relation == "信赖"
    lv = favor_level(200)
    assert lv.level == 8 and lv.ceiling is None
    assert lv.progress(200) == 1.0
    assert favor_level(35).progress(35) == 0.5  # 30~40 区间中点
    assert favor_level(35).to_next(35) == 5


async def test_first_sign(social):
    result = await social.sign("10001", "hhk")
    assert result.signed
    assert 0.01 <= result.impression_added <= 5.0
    assert result.coins_added >= 25  # 金币下限 20 + 首日连签加成 5
    assert result.streak_bonus == social.settings.streak_bonus_step
    p = result.profile
    assert p.sign_count == 1 and p.streak == 1 and p.rank == 1
    assert p.impression == result.impression_added
    # 建档送初始金币 + 签到所得
    assert p.coins == social.settings.initial_coins + result.coins_added


async def test_second_sign_same_day(social):
    first = await social.sign("10001", "hhk")
    again = await social.sign("10001", "hhk")
    assert not again.signed
    assert again.impression_added == 0.0
    assert again.profile.coins == first.profile.coins


async def test_streak_continues_and_resets(social, db):
    from sqlalchemy import update

    from botcchi.db.models import SocialUserRow

    await social.sign("10001", "hhk")

    async def backdate(days_ago: int):
        async with db.session() as s:
            await s.execute(
                update(SocialUserRow)
                .where(SocialUserRow.user_id == "10001")
                .values(
                    last_sign_date=(today() - timedelta(days=days_ago)).isoformat()
                )
            )
            await s.commit()

    await backdate(1)  # 昨天签过 → 连签 +1
    result = await social.sign("10001", "hhk")
    assert result.profile.streak == 2

    await backdate(3)  # 断签 → 重置为 1
    result = await social.sign("10001", "hhk")
    assert result.profile.streak == 1


async def test_coin_ledger(social):
    balance = await social.add_coins("10001", 100, "test")
    assert balance == 300  # 初始 200 + 100
    assert await social.spend_coins("10001", 30, "shop")
    assert not await social.spend_coins("10001", 1000, "shop")
    p = await social.profile("10001")
    assert p.coins == 270


async def test_favor_block_and_peek(social):
    assert await social.favor_block("999", "陌生人") == ""
    assert await social.peek("999") is None
    await social.sign("10001", "hhk")
    block = await social.favor_block("10001", "hhk")
    assert "好感度" in block and "路人" in block and "hhk" in block


def make_tool_ctx(channel, user_id="10001", user_name="hhk") -> ToolContext:
    session = SessionKey(channel="fake", chat_type="private", chat_id=user_id)
    incoming = IncomingMessage(
        session=session, user_id=user_id, user_name=user_name, text="", at_me=True
    )
    return ToolContext(incoming=incoming, channel=channel, bot_name="波奇")


@pytest.fixture
def social_tools(social, monkeypatch):
    from botcchi.config import RenderSettings
    from botcchi.social.card import SocialCards

    async def no_avatar(self, user_id):
        return None

    monkeypatch.setattr(SocialCards, "_fetch_avatar", no_avatar)
    cards = SocialCards(SvgRenderer(RenderSettings()))
    return build_social_tools(social, cards)


async def test_sign_in_tool_with_context(social, social_tools):
    sign_in, get_user = social_tools
    assert sign_in.needs_context and get_user.needs_context
    # ctx 不暴露给 LLM 的参数 schema
    assert "ctx" not in sign_in.definition()["function"]["parameters"]["properties"]
    # 无上下文调用被拒（伪人等场景防线）
    assert "会话上下文" in await sign_in.execute("")

    channel = ImageChannel()
    out = await sign_in.execute("", make_tool_ctx(channel))
    assert "签到成功" in out and "卡片已发送" in out
    assert len(channel.images) == 1
    assert (await social.peek("10001")).sign_count == 1

    # 重复签到：不再发卡片，返回信息供模型转述
    out2 = await sign_in.execute("", make_tool_ctx(channel))
    assert "已经签到过" in out2 and "本次未发送" in out2
    assert len(channel.images) == 1


async def test_get_user_social_tool_defaults_to_self(social, social_tools):
    _, get_user = social_tools
    await social.sign("10001", "hhk")
    channel = ImageChannel()
    # 留空 user_id → 查当前对话用户
    assert "hhk" in await get_user.execute("{}", make_tool_ctx(channel))
    assert "还没有签到" in await get_user.execute(
        '{"user_id": "999"}', make_tool_ctx(channel)
    )


def test_context_tools_hidden_without_ctx(social_tools):
    registry = ToolRegistry()
    for t in social_tools:
        registry.register(t)
    names = ["sign_in", "get_user_social"]
    assert len(registry.definitions(names)) == 2
    assert registry.definitions(names, include_context_tools=False) == []


async def test_rank_ordering(social, db):
    from sqlalchemy import update

    from botcchi.db.models import SocialUserRow

    for uid in ("1", "2", "3"):
        await social.sign(uid, f"u{uid}")
    async with db.session() as s:
        await s.execute(
            update(SocialUserRow)
            .where(SocialUserRow.user_id == "2")
            .values(impression=99.0)
        )
        await s.commit()
    top = await social.top()
    assert top[0].user_id == "2" and top[0].rank == 1
    assert (await social.peek("2")).rank == 1


def test_card_svg_modes_and_escaping():
    from botcchi.social.service import SocialProfile

    profile = SocialProfile(
        user_id="10001",
        user_name='<小明> & "同学"',
        impression=33.7,
        coins=120,
        sign_count=3,
        streak=2,
        last_sign_date="2026-07-17",
        rank=1,
    )
    sign_svg = build_card(
        CardData(
            profile=profile, mode="sign", message="hi", theme="pink",
            impression_added=1.5, coins_added=60, streak_bonus=10, double=True,
        )
    )
    assert "<小明>" not in sign_svg and "&lt;小明&gt;" in sign_svg
    assert "+1.50" in sign_svg and "+60" in sign_svg and "双倍!" in sign_svg
    assert "今日签到" in sign_svg and "累计签到3天" in sign_svg
    assert "好感度等级: 4" in sign_svg and "普通朋友" in sign_svg  # 33.7 → Lv.4

    profile_svg = build_card(
        CardData(profile=profile, mode="profile", message="hi", theme="pink")
    )
    assert "我的信息" in profile_svg
    assert "排名: 1" in profile_svg and "2026-07-17" in profile_svg

    dark_svg = build_card(
        CardData(
            profile=profile, mode="sign", message="hi", theme="dark",
            impression_added=1.5, coins_added=60,
        )
    )
    assert "今日签到" in dark_svg and "累计签到3天" in dark_svg
    assert "好感度等级: 4 · 普通朋友" in dark_svg
    # 暗色为简化布局：无吊牌/天气/立绘/章鱼贴纸，内嵌图片明显少于粉色版
    assert dark_svg.count("<image") < sign_svg.count("<image")


def test_theme_by_hour():
    from botcchi.social.card import resolve_theme

    assert resolve_theme(0) == "dark"
    assert resolve_theme(5) == "dark"
    assert resolve_theme(6) == "pink"
    assert resolve_theme(12) == "pink"
    assert resolve_theme(17) == "pink"
    assert resolve_theme(18) == "dark"
    assert resolve_theme(23) == "dark"


def test_svg_renderer_produces_png():
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><text>{esc("你好<&>")}</text></svg>'
    png = SvgRenderer().to_png(svg)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert to_onebot_file(png).startswith("base64://")


class ImageChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.images: list[tuple[SessionKey, str]] = []

    async def send_image(self, session, file):
        self.images.append((session, file))


async def test_sign_command_sends_card(mock_openai, tmp_path, monkeypatch):
    from botcchi.app import build_context
    from botcchi.social.card import SocialCards

    async def no_avatar(self, user_id):
        return None

    monkeypatch.setattr(SocialCards, "_fetch_avatar", no_avatar)

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()
    service = ctx.service
    assert service.social is not None

    channel = ImageChannel()
    session = SessionKey(channel="fake", chat_type="private", chat_id="10001")

    def msg(text: str) -> IncomingMessage:
        return IncomingMessage(
            session=session, user_id="10001", user_name="hhk", text=text, at_me=True
        )

    await service.handle(msg("签到"), channel)
    assert len(channel.images) == 1
    assert channel.images[0][1].startswith("base64://")
    assert channel.sent == []  # 出图成功，无文本退化

    # 当天重复签到：不再出卡，只有文字提示
    await service.handle(msg("签到"), channel)
    assert len(channel.images) == 1
    assert len(channel.sent) == 1 and "签过" in channel.sent[0][1]

    await service.handle(msg("我的信息"), channel)
    assert len(channel.images) == 2

    await service.handle(msg("好感度排行"), channel)
    assert len(channel.sent) == 2 and "好感度排行" in channel.sent[-1][1]
    await ctx.shutdown()


async def test_sign_command_falls_back_to_text(mock_openai, tmp_path):
    from botcchi.app import build_context

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()

    channel = FakeChannel()  # 不支持 send_image
    session = SessionKey(channel="fake", chat_type="private", chat_id="10001")
    await ctx.service.handle(
        IncomingMessage(
            session=session, user_id="10001", user_name="hhk", text="签到", at_me=True
        ),
        channel,
    )
    assert len(channel.sent) == 1 and "签到成功" in channel.sent[0][1]
    await ctx.shutdown()


async def test_favor_injected_into_system_prompt(mock_openai, tmp_path):
    from conftest import text_chunks

    from botcchi.app import build_context

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()

    session = SessionKey(channel="fake", chat_type="private", chat_id="10001")
    channel = FakeChannel()
    await ctx.service.social.sign("10001", "hhk")

    mock_openai.enqueue(text_chunks("你来啦！"))
    await ctx.service.handle(
        IncomingMessage(
            session=session, user_id="10001", user_name="hhk", text="在吗", at_me=True
        ),
        channel,
    )
    system = mock_openai.requests[-1]["messages"][0]["content"]
    assert "[好感度]" in system and "hhk" in system
    await ctx.shutdown()


async def test_natural_language_sign_end_to_end(mock_openai, tmp_path, monkeypatch):
    """「波奇帮我签个到」→ LLM 调 sign_in 工具 → 发卡片 → 自然语言回复。"""
    from conftest import text_chunks, tool_call_chunks

    from botcchi.app import build_context
    from botcchi.config import ProviderSettings, Settings
    from botcchi.social.card import SocialCards

    async def no_avatar(self, user_id):
        return None

    monkeypatch.setattr(SocialCards, "_fetch_avatar", no_avatar)

    (tmp_path / "test.yaml").write_text(
        "name: test\n"
        "display_name: 波奇\n"
        "model: mock/m\n"
        "prompt: 你是波奇\n"
        "tools: [sign_in, get_user_social]\n"
        "policy:\n  split: false\n",
        encoding="utf-8",
    )
    settings = Settings(
        providers=[
            ProviderSettings(name="mock", api_base="http://mock/v1", api_keys=["k"])
        ],
        default_assistant="test",
        assistants_dir=tmp_path,
    )
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()

    mock_openai.enqueue(tool_call_chunks("sign_in", "{}"))
    mock_openai.enqueue(text_chunks("签好啦！今天也要加油哦"))

    channel = ImageChannel()
    session = SessionKey(channel="fake", chat_type="private", chat_id="10001")
    await ctx.service.handle(
        IncomingMessage(
            session=session, user_id="10001", user_name="hhk",
            text="波奇帮我签个到", at_me=True,
        ),
        channel,
    )

    # 首次请求下发了 sign_in 工具定义
    tools_sent = [t["function"]["name"] for t in mock_openai.requests[0]["tools"]]
    assert "sign_in" in tools_sent
    # 卡片图已发出，工具结果回填后模型自然语言收尾
    assert len(channel.images) == 1
    assert any("签好啦" in text for _, text, _ in channel.sent)
    assert "tool" in [m["role"] for m in mock_openai.requests[-1]["messages"]]
    # 签到确实落库
    assert (await ctx.service.social.peek("10001")).sign_count == 1
    await ctx.shutdown()


async def test_wake_word_in_group_can_call_tools(mock_openai, tmp_path, monkeypatch):
    """群里「波奇帮我签个到」（不 @）也应携带工具上下文并可调用 sign_in。"""
    from conftest import text_chunks, tool_call_chunks

    from botcchi.app import build_context
    from botcchi.config import ProviderSettings, Settings, TriggerSettings
    from botcchi.social.card import SocialCards

    async def no_avatar(self, user_id):
        return None

    monkeypatch.setattr(SocialCards, "_fetch_avatar", no_avatar)

    (tmp_path / "test.yaml").write_text(
        "name: test\n"
        "display_name: 波奇\n"
        "model: mock/m\n"
        "prompt: 你是波奇\n"
        "tools: [sign_in]\n"
        "policy:\n  split: false\n",
        encoding="utf-8",
    )
    settings = Settings(
        providers=[
            ProviderSettings(name="mock", api_base="http://mock/v1", api_keys=["k"])
        ],
        default_assistant="test",
        assistants_dir=tmp_path,
        trigger=TriggerSettings(group_reply_rate=0.0, wake_words=["波奇"]),
    )
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()

    mock_openai.enqueue(tool_call_chunks("sign_in", "{}"))
    mock_openai.enqueue(text_chunks("签好啦！"))

    channel = ImageChannel()
    group = SessionKey(channel="fake", chat_type="group", chat_id="777")
    await ctx.service.handle(
        IncomingMessage(
            session=group, user_id="10001", user_name="hhk",
            text="波奇帮我签个到", at_me=False,
        ),
        channel,
    )

    tools_sent = [t["function"]["name"] for t in mock_openai.requests[0]["tools"]]
    assert "sign_in" in tools_sent
    assert len(channel.images) == 1
    assert (await ctx.service.social.peek("10001")).sign_count == 1
    await ctx.shutdown()
