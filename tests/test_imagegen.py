"""GPT 绘图：生图/改图请求、预设、冷却、错误分类、指令与工具接入。"""

import base64
import json

import httpx
from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.config import ImageGenSettings
from botcchi.imagegen import PRESETS, ImageGenService

FAKE_PNG = b"\x89PNGdrawn"
B64 = base64.b64encode(FAKE_PNG).decode()


class ImageChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.images: list[str] = []

    async def send_image(self, session, file):
        self.images.append(file)


def make_service(status=200, body=None, **settings_kw) -> ImageGenService:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "qq-image" in str(request.url):
            return httpx.Response(200, content=b"INPUTIMG")
        if status != 200:
            return httpx.Response(status, json=body or {})
        return httpx.Response(
            200, json=body or {"data": [{"b64_json": B64}]}
        )

    settings = ImageGenSettings(
        **{
            "api_base": "http://relay.example/v1",
            "api_key": "sk-test",
            "max_retries": 0,
            "cooldown_seconds": 0,
            **settings_kw,
        }
    )
    service = ImageGenService(settings)
    transport = httpx.MockTransport(handler)
    service.client = httpx.AsyncClient(
        base_url="http://relay.example/v1",
        transport=transport,
        headers={"Authorization": "Bearer sk-test"},
    )
    service.download_client = httpx.AsyncClient(transport=transport)
    service.requests = requests  # type: ignore[attr-defined]
    return service


def msg(text="", images=None, user_id="10001") -> IncomingMessage:
    return IncomingMessage(
        session=SessionKey(channel="fake", chat_type="group", chat_id="777"),
        user_id=user_id,
        user_name="hhk",
        text=text,
        images=images or [],
        at_me=True,
    )


def test_parse_preset():
    service = make_service()
    assert service.parse_preset("一只猫") == (PRESETS["默认"], "一只猫")
    assert service.parse_preset("竖图 一只猫") == (PRESETS["竖图"], "一只猫")
    assert service.parse_preset("高清一只猫") == (PRESETS["高清"], "一只猫")


async def test_generate_request_shape():
    service = make_service()
    channel = ImageChannel()
    out = await service.draw_for(msg(), channel, "赛博朋克橘猫", PRESETS["竖图"])
    assert out.startswith("已完成生图") and "1024x1536" in out
    assert channel.images == [f"base64://{B64}"]
    req = service.requests[-1]
    assert str(req.url).endswith("/images/generations")
    assert req.headers["Authorization"] == "Bearer sk-test"
    payload = json.loads(req.content)
    assert payload["prompt"] == "赛博朋克橘猫"
    assert payload["size"] == "1024x1536" and payload["quality"] == "low"
    assert payload["output_compression"] == 85  # jpeg 默认带压缩参数
    await service.aclose()


async def test_edit_with_input_images():
    service = make_service()
    channel = ImageChannel()
    out = await service.draw_for(
        msg(images=["http://qq-image.example/a.jpg"]),
        channel,
        "改成二次元风",
        PRESETS["默认"],
    )
    assert out.startswith("已完成改图")
    req = service.requests[-1]
    assert str(req.url).endswith("/images/edits")
    assert b"INPUTIMG" in req.content  # multipart 里带了下载的输入图
    await service.aclose()


async def test_error_classification():
    # 内容安全拒绝
    service = make_service(
        status=400, body={"error": {"message": "rejected by safety policy"}}
    )
    out = await service.draw_for(msg(), ImageChannel(), "x", PRESETS["默认"])
    assert "内容安全" in out
    await service.aclose()

    # 限流
    service = make_service(status=429)
    out = await service.draw_for(msg(), ImageChannel(), "x", PRESETS["默认"])
    assert "限流" in out
    await service.aclose()

    # 普通 400 透出 message
    service = make_service(
        status=400, body={"error": {"message": "invalid size"}}
    )
    out = await service.draw_for(msg(), ImageChannel(), "x", PRESETS["默认"])
    assert "invalid size" in out
    await service.aclose()


async def test_cooldown_and_prompt_validation():
    service = make_service(cooldown_seconds=300)
    channel = ImageChannel()
    assert (
        await service.draw_for(msg(), channel, "  ", PRESETS["默认"])
        == "请给出要画什么的描述"
    )
    out = await service.draw_for(msg(), channel, "第一张", PRESETS["默认"])
    assert out.startswith("已完成")
    out = await service.draw_for(msg(), channel, "第二张", PRESETS["默认"])
    assert "冷却中" in out
    # 其他用户不受影响；skip_cooldown（超管）也不受影响
    out = await service.draw_for(
        msg(user_id="20002"), channel, "别人的", PRESETS["默认"]
    )
    assert out.startswith("已完成")
    out = await service.draw_for(
        msg(), channel, "超管的", PRESETS["默认"], skip_cooldown=True
    )
    assert out.startswith("已完成")
    await service.aclose()


async def test_command_and_tool_integration(mock_openai, tmp_path):
    from botcchi.app import build_context
    from botcchi.tools.registry import ToolContext

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    settings.onebot.superusers = ["900000"]
    settings.imagegen.api_base = "http://relay.example/v1"
    settings.imagegen.api_key = "sk-test"
    settings.imagegen.cooldown_seconds = 0
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()
    # 注入 mock 网络
    mock_svc = make_service()
    await ctx.service.imagegen.aclose()
    ctx.service.imagegen.client = mock_svc.client
    ctx.service.imagegen.download_client = mock_svc.download_client

    channel = ImageChannel()
    await ctx.service.handle(msg("画图 帮助"), channel)
    assert "画图帮助" in channel.sent[-1][1]

    await ctx.service.handle(msg("画图 横图 一只猫"), channel)
    assert any("正在画图" in t for _, t, _ in channel.sent)
    assert channel.images and channel.images[-1] == f"base64://{B64}"

    # 工具路径
    drawn = len(channel.images)
    tool = ctx.service.loop.tools.get("draw_image")
    out = await tool.execute(
        '{"prompt": "星空下的少女", "orientation": "portrait"}',
        ToolContext(incoming=msg(), channel=channel, bot_name="波奇"),
    )
    assert out.startswith("已完成生图")
    assert len(channel.images) == drawn + 1

    # 功能开关拦截
    block = msg("关闭功能 绘图")
    block.user_id = "900000"
    await ctx.service.handle(block, channel)
    await ctx.service.handle(msg("画图 一只狗"), channel)
    assert "「绘图」在本群已被关闭" in channel.sent[-1][1]
    await ctx.shutdown()


async def test_imagegen_disabled_without_config(mock_openai, tmp_path):
    from botcchi.app import build_context

    ctx = build_context(make_settings(tmp_path), client=mock_openai.client())
    assert ctx.service.imagegen is None
    assert ctx.service.loop.tools.get("draw_image") is None
    channel = FakeChannel()
    await ctx.service.handle(msg("画图 一只猫"), channel)
    assert "未配置绘图接口" in channel.sent[-1][1]
    await ctx.shutdown()
