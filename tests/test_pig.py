"""小猪：确定性日抽、占卜卡片、PigHub 随机/搜索、合并转发与退化。"""

import httpx
from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.config import RenderSettings
from botcchi.pig import PIGS, PigService, build_pig_card
from botcchi.render import SvgRenderer

HUB = {
    "images": [
        {"title": "开心猪", "thumbnail": "/thumb/happy.jpg"},
        {"title": "伤心猪", "thumbnail": "/thumb/sad.jpg"},
        {"title": "开心飞猪", "thumbnail": "/thumb/fly.jpg"},
    ]
}


def make_service() -> PigService:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/all-images" in str(request.url):
            return httpx.Response(200, json=HUB)
        return httpx.Response(404)

    return PigService(
        SvgRenderer(RenderSettings()),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_daily_pig_deterministic():
    assert len(PIGS) == 62
    a = PigService.daily_pig("10001")
    assert all(PigService.daily_pig("10001") == a for _ in range(5))
    # 不同用户的分布确实在变（62 选 1，20 个用户全撞同一只概率约 0）
    others = {PigService.daily_pig(str(i)).id for i in range(20)}
    assert len(others) > 1


def test_pig_card_renders():
    svg = build_pig_card(PIGS[0])
    assert "今日小猪占卜" in svg and PIGS[0].name in svg
    assert "<image" in svg  # 小猪立绘已内嵌
    png = SvgRenderer(RenderSettings()).to_png(svg)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def msg(text: str, user_id: str = "10001") -> IncomingMessage:
    return IncomingMessage(
        session=SessionKey(channel="fake", chat_type="group", chat_id="777"),
        user_id=user_id,
        user_name="hhk",
        text=text,
    )


class ImageChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.images: list[str] = []
        self.forwards: list[list] = []

    async def send_image(self, session, file):
        self.images.append(file)


class ForwardChannel(ImageChannel):
    async def send_forward(self, session, nodes):
        self.forwards.append(nodes)


async def test_commands(mock_openai, tmp_path):
    from botcchi.app import build_context

    ctx = build_context(make_settings(tmp_path), client=mock_openai.client())
    mock_svc = make_service()
    await ctx.service.pig.client.aclose()
    ctx.service.pig.client = mock_svc.client
    ctx.service.pig.cache = None  # 测试不写全局缓存目录

    channel = ForwardChannel()
    await ctx.service.handle(msg("今日小猪"), channel)
    assert len(channel.images) == 1
    assert channel.images[0].startswith("base64://")

    # 单张：标题 + 图
    await ctx.service.handle(msg("随机小猪"), channel)
    assert len(channel.images) == 2
    assert channel.images[-1].startswith("https://pighub.top/data/")
    # 多张：合并转发
    await ctx.service.handle(msg("随机小猪 3"), channel)
    assert len(channel.forwards) == 1 and len(channel.forwards[0]) == 3

    # 找猪：关键词过滤
    await ctx.service.handle(msg("找猪 开心"), channel)
    assert len(channel.forwards) == 2
    titles = [n[0] for n in channel.forwards[-1]]
    assert titles == ["开心猪", "开心飞猪"]
    await ctx.service.handle(msg("找猪 恐龙"), channel)
    assert "离家出走" in channel.sent[-1][1]
    await ctx.service.handle(msg("找猪"), channel)
    assert "用法" in channel.sent[-1][1]
    await ctx.shutdown()


async def test_forward_fallback_sequential(mock_openai, tmp_path):
    from botcchi.app import build_context

    ctx = build_context(make_settings(tmp_path), client=mock_openai.client())
    mock_svc = make_service()
    await ctx.service.pig.client.aclose()
    ctx.service.pig.client = mock_svc.client

    channel = ImageChannel()  # 无 send_forward → 逐条退化
    await ctx.service.handle(msg("随机小猪 3"), channel)
    assert len(channel.images) == 3
    await ctx.shutdown()


async def test_pig_tools(tmp_path):
    from botcchi.pig import build_pig_tools
    from botcchi.render import RenderCache
    from botcchi.tools.registry import ToolContext

    svc = make_service()
    svc.cache = RenderCache(tmp_path / "cards")
    fortune, find = build_pig_tools(svc)
    channel = ForwardChannel()
    ctx = ToolContext(incoming=msg("随便聊聊"), channel=channel)

    out = await fortune.execute("{}", ctx)
    pig = svc.daily_pig("10001")
    assert pig.name in out and "已发送" in out
    assert channel.images and channel.images[0].startswith("base64://")

    out = await find.execute('{"keyword": "开心", "count": 5}', ctx)
    assert "已发送 2 张" in out  # 题库只有 2 张「开心」
    assert len(channel.forwards[-1]) == 2
    out = await find.execute('{"keyword": "恐龙"}', ctx)
    assert "没有找到" in out
    await svc.aclose()


async def test_hub_failure_friendly(mock_openai, tmp_path):
    from botcchi.app import build_context

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    ctx = build_context(make_settings(tmp_path), client=mock_openai.client())
    await ctx.service.pig.client.aclose()
    ctx.service.pig.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    channel = ImageChannel()
    await ctx.service.handle(msg("随机小猪"), channel)
    assert "猪圈空荡荡" in channel.sent[-1][1]
    await ctx.shutdown()
