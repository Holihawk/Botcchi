"""和风天气：城市查询 + 实况/3 日预报/预警/空气质量，出冷暖主题卡片。

付费版专属 API host，JWT（Ed25519，手写签名免引 PyJWT）或 api_key 认证。
国内资源直连。旧版查 7 天，新版按需求收敛到 3 天以减轻渲染压力。
"""

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field

import httpx
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from loguru import logger

from ..config import WeatherSettings
from ..render import SvgRenderer, to_onebot_file
from ..tools.registry import Tool, ToolContext, tool

RENDER_ZOOM = 1.5
"""天气卡内容密度不高，1.5 倍在清晰度与体积间平衡"""


class WeatherError(Exception):
    """查询失败（面向用户的消息）。"""


class CityNotFound(WeatherError):
    pass


# ---- JWT ----------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(sub: str, kid: str, private_key_pem: str) -> str:
    """和风天气的 EdDSA JWT（私钥支持 \\n 转义的单行 PEM）。"""
    pem = private_key_pem.replace("\\n", "\n").encode()
    key = load_pem_private_key(pem, password=None)
    now = int(time.time())
    header = _b64url(
        json.dumps({"alg": "EdDSA", "kid": kid}, separators=(",", ":")).encode()
    )
    payload = _b64url(
        json.dumps(
            {"sub": sub, "iat": now - 30, "exp": now + 900},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}"
    signature = key.sign(signing_input.encode())  # type: ignore[call-arg]
    return f"{signing_input}.{_b64url(signature)}"


# ---- 数据模型 ------------------------------------------------------------

AQI_COLORS = {
    "优": "#95B865",
    "良": "#A9A538",
    "轻度污染": "#E0991D",
    "中度污染": "#D96161",
    "重度污染": "#A257D0",
    "严重污染": "#D02C7A",
}
_AQI_CATEGORY_EN = {
    "Good": "优",
    "Moderate": "良",
    "Unhealthy for Sensitive Groups": "轻度污染",
    "Unhealthy": "中度污染",
    "Very Unhealthy": "重度污染",
    "Hazardous": "严重污染",
}


@dataclass(slots=True)
class DailyForecast:
    date: str
    """YYYY-MM-DD"""
    text_day: str
    text_night: str
    temp_min: int
    temp_max: int
    icon: str
    """和风图标代码（白天）"""


@dataclass(slots=True)
class WeatherData:
    city: str
    obs_time: str
    """观测时间 HH:MM"""
    temp: int
    text: str
    icon: str
    feels_like: int
    humidity: str
    wind_dir: str
    wind_scale: str
    daily: list[DailyForecast] = field(default_factory=list)
    aqi: str = ""
    aqi_category: str = ""
    warnings: list[str] = field(default_factory=list)


class WeatherService:
    def __init__(
        self,
        settings: WeatherSettings,
        renderer: SvgRenderer,
        client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.renderer = renderer
        self.client = client or httpx.AsyncClient(
            base_url=settings.api_host.rstrip("/"),
            trust_env=False,
            timeout=15.0,
        )
        self._token = ""
        self._token_exp = 0.0

    async def aclose(self) -> None:
        await self.client.aclose()

    def _headers(self) -> dict[str, str]:
        if not self.settings.api_key:
            if time.time() > self._token_exp - 60:
                self._token = make_jwt(
                    self.settings.jwt_sub,
                    self.settings.jwt_kid,
                    self.settings.jwt_private_key,
                )
                self._token_exp = time.time() + 900
            return {"Authorization": f"Bearer {self._token}"}
        return {"X-QW-Api-Key": self.settings.api_key}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        resp = await self.client.get(path, params=params, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    # ---- 拉取 -----------------------------------------------------------

    async def fetch(self, city: str) -> WeatherData:
        lookup = await self._get(
            "/geo/v2/city/lookup", {"location": city, "number": 1}
        )
        if lookup.get("code") == "404" or not lookup.get("location"):
            raise CityNotFound(f"找不到叫「{city}」的地方…")
        if lookup.get("code") != "200":
            raise WeatherError(f"城市查询失败（code={lookup.get('code')}）")
        loc = lookup["location"][0]
        city_id, name = loc["id"], loc["name"]
        lat, lon = loc.get("lat", ""), loc.get("lon", "")

        now_body, daily_body, air_body, warn_body = await asyncio.gather(
            self._get("/v7/weather/now", {"location": city_id}),
            self._get("/v7/weather/3d", {"location": city_id}),
            self._safe_get(f"/airquality/v1/current/{lat}/{lon}"),
            self._safe_get("/v7/warning/now", {"location": city_id}),
        )
        if now_body.get("code") != "200" or daily_body.get("code") != "200":
            raise WeatherError(
                f"天气查询失败（now={now_body.get('code')} "
                f"daily={daily_body.get('code')}）"
            )
        now = now_body["now"]
        data = WeatherData(
            city=name,
            obs_time=now.get("obsTime", "")[11:16],
            temp=int(now["temp"]),
            text=now.get("text", ""),
            icon=str(now.get("icon", "")),
            feels_like=int(now.get("feelsLike", now["temp"])),
            humidity=now.get("humidity", ""),
            wind_dir=now.get("windDir", ""),
            wind_scale=now.get("windScale", ""),
            daily=[
                DailyForecast(
                    date=d.get("fxDate", ""),
                    text_day=d.get("textDay", ""),
                    text_night=d.get("textNight", ""),
                    temp_min=int(d.get("tempMin", 0)),
                    temp_max=int(d.get("tempMax", 0)),
                    icon=str(d.get("iconDay", "")),
                )
                for d in daily_body.get("daily", [])[:3]
            ],
        )
        # 空气质量与预警是可选增强，失败不影响主体
        if air_body and (indexes := air_body.get("indexes")):
            idx = next(
                (i for i in indexes if i.get("code") == "us-epa"), indexes[0]
            )
            data.aqi = str(idx.get("aqiDisplay") or idx.get("aqi") or "")
            data.aqi_category = _AQI_CATEGORY_EN.get(
                idx.get("category", ""), idx.get("category", "")
            )
        if warn_body and warn_body.get("code") == "200":
            data.warnings = [
                w.get("title", "")
                for w in warn_body.get("warning", [])
                if w.get("title")
            ][:2]
        return data

    async def _safe_get(self, path: str, params: dict | None = None) -> dict:
        try:
            return await self._get(path, params)
        except Exception:
            logger.warning(f"天气附加数据获取失败: {path}")
            return {}

    # ---- 出图 -----------------------------------------------------------

    async def card_png(self, city: str) -> bytes:
        from .card import build_weather_card

        data = await self.fetch(city)
        return await self.renderer.render(
            build_weather_card(data), zoom=RENDER_ZOOM
        )

    def summary(self, data: WeatherData) -> str:
        parts = [
            f"{data.city}：{data.text} {data.temp}°C（体感 {data.feels_like}°C），"
            f"湿度 {data.humidity}%，{data.wind_dir}{data.wind_scale}级"
        ]
        if data.aqi:
            parts.append(f"空气 {data.aqi_category} AQI {data.aqi}")
        for d in data.daily:
            parts.append(f"{d.date[5:]} {d.text_day} {d.temp_min}~{d.temp_max}°C")
        if data.warnings:
            parts.append("预警：" + "；".join(data.warnings))
        return "；".join(parts)


def build_weather_tool(service: WeatherService) -> Tool:
    @tool(
        name="get_weather",
        description=(
            "查询国内城市的实时天气与未来 3 天预报，把天气卡片图发到当前"
            "聊天。用户问天气/该穿什么/要不要带伞时调用；city 传城市或"
            "区县名（如「南京」「浦口」）。返回的文字摘要供你参考作答，"
            "不要照抄，也不用复述卡片数字。"
        ),
    )
    async def get_weather(ctx: ToolContext, city: str) -> str:
        try:
            data = await service.fetch(city)
            from .card import build_weather_card

            png = await service.renderer.render(
                build_weather_card(data), zoom=RENDER_ZOOM
            )
        except CityNotFound as e:
            return str(e)
        except WeatherError as e:
            return f"天气查询失败：{e}"
        except Exception:
            logger.exception("天气卡片生成失败")
            return "天气查询失败了，稍后再试试"
        try:
            await ctx.channel.send_image(
                ctx.incoming.session, to_onebot_file(png)
            )
        except NotImplementedError:
            return f"当前渠道不支持发图。天气：{service.summary(data)}"
        return f"天气卡片已发送。{service.summary(data)}"

    return get_weather


def build_weather_command(service: WeatherService, gate):
    """「天气 <城市> / <城市>天气」指令（后缀形式不经指令前缀功能门，单独查）"""
    import re as _re

    from loguru import logger

    from ..commands import Command
    from .card import build_weather_card

    pattern = _re.compile(r"^天气\s*(?P<a>\S{1,15})$|^(?P<b>\S{1,15})天气$")

    def match(text: str) -> str | None:
        if m := pattern.fullmatch(text):
            return m.group("a") or m.group("b")
        return None

    async def weather_cmd(incoming, channel, city: str) -> bool:
        session = incoming.session
        if session.chat_type == "group" and await gate.feature_blocked(
            session.chat_id, "天气"
        ):
            await channel.send_text(session, "「天气」在本群已被关闭")
            return True
        try:
            data = await service.fetch(city)
            png = await service.renderer.render(
                build_weather_card(data), zoom=RENDER_ZOOM
            )
        except CityNotFound as e:
            await channel.send_text(session, str(e))
            return True
        except WeatherError as e:
            await channel.send_text(session, str(e))
            return True
        except Exception:
            logger.exception(f"天气查询失败: {city}")
            await channel.send_text(session, "天气查不到了…稍后再试试吧")
            return True
        try:
            await channel.send_image(session, to_onebot_file(png))
        except Exception:
            # 发图失败（渠道不支持/发送异常）都退化为文字摘要
            logger.exception("天气卡片发送失败，退化为文本")
            await channel.send_text(session, service.summary(data))
        return True

    return Command(match, weather_cmd)
