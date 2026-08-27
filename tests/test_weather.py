"""和风天气：JWT 签名、数据拉取、冷暖主题、卡片与指令/工具接入。"""

import base64
import json

import httpx
import pytest
from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.config import RenderSettings, WeatherSettings
from botcchi.render import SvgRenderer
from botcchi.weather import (
    CityNotFound,
    WeatherData,
    WeatherService,
    make_jwt,
)
from botcchi.weather.card import (
    COLD,
    WARM,
    build_weather_card,
    icon_kind,
    resolve_theme,
)

LOOKUP = {
    "code": "200",
    "location": [
        {"id": "101190101", "name": "南京", "lat": "32.06", "lon": "118.79"}
    ],
}
NOW = {
    "code": "200",
    "now": {
        "obsTime": "2026-07-19T15:20+08:00",
        "temp": "33",
        "feelsLike": "36",
        "text": "晴",
        "icon": "100",
        "humidity": "58",
        "windDir": "东南风",
        "windScale": "3",
    },
}
DAILY = {
    "code": "200",
    "daily": [
        {"fxDate": "2026-07-19", "textDay": "晴", "textNight": "晴",
         "tempMin": "26", "tempMax": "35", "iconDay": "100"},
        {"fxDate": "2026-07-20", "textDay": "多云", "textNight": "小雨",
         "tempMin": "25", "tempMax": "33", "iconDay": "101"},
        {"fxDate": "2026-07-21", "textDay": "中雨", "textNight": "中雨",
         "tempMin": "23", "tempMax": "28", "iconDay": "306"},
    ],
}
AIR = {
    "indexes": [
        {"code": "us-epa", "aqiDisplay": "54", "category": "Moderate"}
    ]
}
WARNING = {
    "code": "200",
    "warning": [{"title": "江苏省气象台发布高温橙色预警"}],
}


def make_service(city_404=False) -> WeatherService:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        if "/geo/v2/city/lookup" in url:
            if city_404:
                return httpx.Response(200, json={"code": "404"})
            return httpx.Response(200, json=LOOKUP)
        if "/v7/weather/now" in url:
            return httpx.Response(200, json=NOW)
        if "/v7/weather/3d" in url:
            return httpx.Response(200, json=DAILY)
        if "/airquality/v1/current/" in url:
            return httpx.Response(200, json=AIR)
        if "/v7/warning/now" in url:
            return httpx.Response(200, json=WARNING)
        return httpx.Response(404)

    settings = weather_settings()
    return WeatherService(
        settings,
        SvgRenderer(RenderSettings()),
        client=httpx.AsyncClient(
            base_url=settings.api_host,
            transport=httpx.MockTransport(handler),
        ),
    )


def weather_settings() -> WeatherSettings:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    pem = (
        Ed25519PrivateKey.generate()
        .private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        .decode()
    )
    return WeatherSettings(
        api_host="https://test.re.qweatherapi.com",
        jwt_sub="PROJ123",
        jwt_kid="KID456",
        jwt_private_key=pem.replace("\n", "\\n"),  # 模拟单行转义存储
    )


def test_jwt_structure_and_signature():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    ).decode()
    token = make_jwt("SUB1", "KID1", pem.replace("\n", "\\n"))
    head_b64, payload_b64, sig_b64 = token.split(".")

    def unb64(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    header = json.loads(unb64(head_b64))
    payload = json.loads(unb64(payload_b64))
    assert header == {"alg": "EdDSA", "kid": "KID1"}
    assert payload["sub"] == "SUB1" and payload["exp"] > payload["iat"]
    # 公钥可验签
    key.public_key().verify(
        unb64(sig_b64), f"{head_b64}.{payload_b64}".encode()
    )


async def test_fetch_full_data():
    service = make_service()
    data = await service.fetch("南京")
    assert data.city == "南京" and data.temp == 33 and data.text == "晴"
    assert data.obs_time == "15:20" and data.feels_like == 36
    assert len(data.daily) == 3
    assert data.daily[2].text_day == "中雨" and data.daily[2].temp_max == 28
    assert data.daily[1].text_day == "多云" and data.daily[1].text_night == "小雨"
    assert data.aqi == "54" and data.aqi_category == "良"
    assert data.warnings == ["江苏省气象台发布高温橙色预警"]
    summary = service.summary(data)
    assert "南京" in summary and "高温橙色预警" in summary
    await service.aclose()


async def test_city_not_found():
    service = make_service(city_404=True)
    with pytest.raises(CityNotFound, match="不存在的城市"):
        await service.fetch("不存在的城市")
    await service.aclose()


def test_theme_selection():
    assert resolve_theme("100", 30) is WARM  # 晴热
    assert resolve_theme("101", 15) is WARM  # 多云不冷
    assert resolve_theme("100", 5) is COLD  # 晴但冷
    assert resolve_theme("306", 25) is COLD  # 下雨
    assert resolve_theme("104", 20) is COLD  # 阴
    assert resolve_theme("401", 0) is COLD  # 雪

    assert icon_kind("100") == "sunny"
    assert icon_kind("104") == "overcast"
    assert icon_kind("302") == "thunder"
    assert icon_kind("316") == "rain"
    assert icon_kind("499") == "snow"
    assert icon_kind("501") == "fog"


def test_card_svg_content():
    data = WeatherData(
        city="南京", obs_time="15:20", temp=33, text="晴", icon="100",
        feels_like=36, humidity="58", wind_dir="东南风", wind_scale="3",
        daily=[
            __import__("botcchi.weather", fromlist=["DailyForecast"]).DailyForecast(
                date="2026-07-19", text_day="晴", text_night="晴",
                temp_min=26, temp_max=35, icon="100",
            )
        ],
        aqi="54", aqi_category="良",
        warnings=["高温橙色预警"],
    )
    svg = build_weather_card(data)
    assert "南京" in svg and "33°" in svg and "体感 36°" in svg
    assert "AQI 54 良" in svg and "高温橙色预警" in svg
    assert "今天" in svg and "26°" in svg and "35°" in svg
    assert WARM.bg_top in svg  # 晴 33° → 暖色调
    assert "<image" not in svg  # 零图片资产

    # 雨天 → 冷色调
    data.icon, data.text, data.temp = "306", "中雨", 22
    assert COLD.bg_top in build_weather_card(data)

    # 真渲染一次确保 SVG 合法
    png = SvgRenderer(RenderSettings()).to_png(svg)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


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


async def test_command_forms_and_tool(mock_openai, tmp_path):
    from botcchi.app import build_context
    from botcchi.tools.registry import ToolContext

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    settings.onebot.superusers = ["900000"]
    settings.weather = weather_settings()
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()
    mock_svc = make_service()
    await ctx.service.weather.aclose()
    ctx.service.weather.client = mock_svc.client

    channel = ImageChannel()
    await ctx.service.handle(msg("天气 南京"), channel)
    assert len(channel.images) == 1
    await ctx.service.handle(msg("南京天气"), channel)  # 后缀形式
    assert len(channel.images) == 2
    # 闲聊句不触发
    await ctx.service.handle(msg("今天天气真不错啊"), channel)
    assert len(channel.images) == 2

    # 工具路径带摘要返回
    tool = ctx.service.loop.tools.get("get_weather")
    out = await tool.execute(
        '{"city": "南京"}',
        ToolContext(incoming=msg(""), channel=channel, bot_name="波奇"),
    )
    assert "已发送" in out and "南京" in out and len(channel.images) == 3

    # 功能开关
    block = msg("关闭功能 天气")
    block.user_id = "900000"
    await ctx.service.handle(block, channel)
    await ctx.service.handle(msg("南京天气"), channel)
    assert "「天气」在本群已被关闭" in channel.sent[-1][1]
    assert len(channel.images) == 3
    await ctx.shutdown()


async def test_weather_disabled_without_config(mock_openai, tmp_path):
    from botcchi.app import build_context

    ctx = build_context(make_settings(tmp_path), client=mock_openai.client())
    assert ctx.service.weather is None
    assert ctx.service.loop.tools.get("get_weather") is None
    await ctx.shutdown()
