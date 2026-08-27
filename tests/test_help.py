"""帮助菜单：主题过滤、编号/名称查找、超管条目隐藏、卡片渲染与指令接入。"""

from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.config import RenderSettings
from botcchi.help import (
    OFF_LINE,
    OFF_TEXT,
    TOPICS,
    build_menu_card,
    build_topic_card,
    find_topic,
    menu_text,
    topic_text,
    visible_topics,
)
from botcchi.render import RenderCache, SvgRenderer


def test_visible_topics_filtering():
    all_topics = visible_topics(None)
    assert [t.key for t in all_topics] == [t.key for t in TOPICS]
    # 只配了数据库相关服务：天气/绘图被隐藏
    some = visible_topics({"social", "summary", "report", "scheduler"})
    keys = [t.key for t in some]
    assert "签到" in keys and "总结" in keys
    assert "天气" not in keys and "绘图" not in keys
    # 无依赖的主题始终在
    assert "闲聊" in keys and "权限" in keys and "解析" in keys


def test_find_topic():
    available = None
    n, t = find_topic("1", available)
    assert n == 1 and t.key == "闲聊"
    n, t = find_topic("天气", available)
    assert t.key == "天气" and n == [x.key for x in TOPICS].index("天气") + 1
    assert find_topic("999", available) is None
    assert find_topic("不存在", available) is None
    # 过滤后编号顺延
    some = {"social"}
    n2, t2 = find_topic("签到", some)
    assert t2.key == "签到"
    assert visible_topics(some)[n2 - 1].key == "签到"


def test_menu_card_and_text():
    topics = visible_topics(None)
    svg = build_menu_card(topics)
    assert "Bocchi The Rock!" in svg
    assert "闲聊" in svg and "权限" in svg
    assert str(len(topics)) in svg  # 最后一个编号
    assert "blob-blur)" not in svg.replace('filter id="blob-blur"', "")  # 无光晕
    png = SvgRenderer(RenderSettings()).to_png(svg)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"

    text = menu_text(topics)
    assert text.startswith("📖") and "1. 闲聊" in text


def test_search_is_not_a_menu_entry():
    # 联网搜索是闲聊时自动触发的基础能力，不占菜单条目
    assert "搜索" not in [t.key for t in TOPICS]
    assert find_topic("搜索", None) is None


def test_menu_card_marks_disabled_features():
    topics = visible_topics(None)
    off = frozenset({"签到", "天气"})
    svg = build_menu_card(topics, disabled=off)
    # 关闭的条目仍列出，但置灰 + 划线，顶栏写「可用/总数」
    assert "签到" in svg and "天气" in svg
    assert svg.count(f'stroke="{OFF_LINE}"') == len(off)
    assert OFF_TEXT in svg
    assert f"{len(topics) - len(off)}/{len(topics)}" in svg
    png = SvgRenderer(RenderSettings()).to_png(svg)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"

    # 全部可用时不出现划线，顶栏只写一个数字
    plain = build_menu_card(topics)
    assert OFF_LINE not in plain
    assert f"{len(topics)}/{len(topics)}" not in plain

    text = menu_text(topics, off)
    assert f"（{len(topics) - len(off)} 个可用功能）" in text
    assert "签到 — 每日签到攒好感度和金币（本群已关闭）" in text
    assert "（本群已关闭）" not in menu_text(topics)


async def test_render_cache_scopes_by_feature(tmp_path):
    """各功能的卡片缓存进各自子目录，淘汰互不干扰。"""
    root = RenderCache(tmp_path / "cards")
    renderer = SvgRenderer(RenderSettings())
    await root.scope("help").render(renderer, build_topic_card(*find_topic("权限", None)))
    assert len(list((tmp_path / "cards" / "help").glob("*.png"))) == 1
    assert not list((tmp_path / "cards").glob("*.png"))


def test_topic_card_hides_superuser_items():
    _, admin = find_topic("群管理", None)
    svg = build_topic_card(1, admin)
    assert "设置欢迎语" in svg and "需管理权限" in svg
    assert "撤回" not in svg  # 仅超管条目隐藏
    png = SvgRenderer(RenderSettings()).to_png(svg)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"

    _, perms = find_topic("权限", None)
    assert "封禁群" not in build_topic_card(2, perms)
    assert "封禁群" not in topic_text(perms)


def msg(text: str) -> IncomingMessage:
    return IncomingMessage(
        session=SessionKey(channel="fake", chat_type="group", chat_id="777"),
        user_id="10001",
        user_name="hhk",
        text=text,
        at_me=True,
    )


class ImageChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.images: list[str] = []

    async def send_image(self, session, file):
        self.images.append(file)


async def test_help_commands(mock_openai, tmp_path):
    from botcchi.app import build_context

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()
    ctx.service.helper.cache = None  # 测试不写全局缓存目录
    channel = ImageChannel()

    await ctx.service.handle(msg("帮助"), channel)
    assert len(channel.images) == 1
    assert channel.images[0].startswith("base64://")
    await ctx.service.handle(msg("菜单"), channel)
    assert len(channel.images) == 2

    await ctx.service.handle(msg("帮助 2"), channel)  # 编号查详情
    assert len(channel.images) == 3
    await ctx.service.handle(msg("帮助 权限"), channel)  # 名称查详情
    assert len(channel.images) == 4

    await ctx.service.handle(msg("帮助 999"), channel)
    assert "没有这个编号" in channel.sent[-1][1]
    # 「帮助我…」不是查功能，不应被帮助指令拦截（交给闲聊）
    before = len(channel.images)
    await ctx.service.handle(msg("帮助我想一句诗"), channel)
    assert len(channel.images) == before
    await ctx.shutdown()
