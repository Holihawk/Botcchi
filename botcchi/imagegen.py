"""GPT 绘图：OpenAI images API 兼容端点的生图/改图。

沿旧版 nonebot_plugin_gpt_image 迁移：默认/竖图/横图/高清四档预设、
带图即改图（images/edits）、每用户冷却、5xx 与网络错误退避重试、
内容安全拒绝的友好识别。URL 和 key 独立于对话 providers 配置。
"""

import asyncio
import base64
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from loguru import logger

from .channels.base import Channel, IncomingMessage
from .config import ImageGenSettings
from .render import to_onebot_file
from .tools.registry import Tool, ToolContext, tool

if TYPE_CHECKING:
    from .perms import PermissionGate


class ImageGenError(Exception):
    """生成失败（面向用户的消息）。"""


class ContentRejected(ImageGenError):
    """疑似触发内容安全限制。"""


@dataclass(frozen=True, slots=True)
class Preset:
    name: str
    size: str
    quality: str


PRESETS: dict[str, Preset] = {
    "默认": Preset("默认", "1024x1024", "low"),
    "竖图": Preset("竖图", "1024x1536", "low"),
    "横图": Preset("横图", "1536x1024", "low"),
    "高清": Preset("高清", "1024x1024", "high"),
}

_DATA_URI = re.compile(r"^data:image/[^;]+;base64,")
_REJECT_KEYS = (
    "safety", "policy", "moderation", "content", "not allowed", "disallowed",
)

HELP_TEXT = (
    "🎨 画图帮助\n"
    "画图 <描述> ｜ 画图 竖图/横图/高清 <描述>\n"
    "带一张图或引用图片再发「画图 <描述>」即为改图\n"
    "也可以直接说「波奇画一只猫」让 AI 代劳"
)


def _build_client(settings: ImageGenSettings) -> httpx.AsyncClient:
    """按代理策略构建客户端（与 provider 同一套语义）。"""
    timeout = httpx.Timeout(
        settings.timeout, connect=20.0, write=60.0, pool=30.0
    )
    headers = {"Authorization": f"Bearer {settings.api_key}"}
    base = settings.api_base.rstrip("/")
    if settings.proxy is None or settings.proxy is True:
        return httpx.AsyncClient(base_url=base, timeout=timeout, headers=headers)
    if settings.proxy is False:
        return httpx.AsyncClient(
            base_url=base, timeout=timeout, headers=headers, trust_env=False
        )
    return httpx.AsyncClient(
        base_url=base, timeout=timeout, headers=headers,
        proxy=settings.proxy, trust_env=False,
    )


class ImageGenService:
    def __init__(self, settings: ImageGenSettings):
        self.settings = settings
        self.client = _build_client(settings)
        # QQ 图床是国内资源：输入图下载强制直连
        self.download_client = httpx.AsyncClient(trust_env=False, timeout=30.0)
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._last_call: dict[str, float] = {}

    async def aclose(self) -> None:
        await self.client.aclose()
        await self.download_client.aclose()

    # ---- 冷却 -----------------------------------------------------------

    def cooldown_remaining(self, user_id: str) -> int:
        """剩余冷却秒数；为 0 时视为本次调用成立并记账。"""
        if self.settings.cooldown_seconds <= 0:
            return 0
        now = time.monotonic()
        remain = int(
            self.settings.cooldown_seconds - (now - self._last_call.get(user_id, -1e9))
        )
        if remain > 0:
            return remain
        self._last_call[user_id] = now
        return 0

    # ---- 生成 -----------------------------------------------------------

    def parse_preset(self, text: str) -> tuple[Preset, str]:
        """剥离预设前缀，返回 (预设, 剩余提示词)"""
        for name in ("竖图", "横图", "高清"):
            if text.startswith(name):
                return PRESETS[name], text.removeprefix(name).strip()
        return PRESETS["默认"], text

    async def fetch_input_images(self, urls: list[str]) -> list[bytes]:
        images: list[bytes] = []
        for url in urls[: self.settings.max_input_images]:
            resp = await self.download_client.get(url)
            resp.raise_for_status()
            images.append(resp.content)
        return images

    async def generate(
        self, prompt: str, preset: Preset, input_images: list[bytes] | None = None
    ) -> bytes:
        """出图；有输入图时走改图接口。返回图片字节"""
        async with self._semaphore:
            if input_images:
                b64 = await self._edit(prompt, input_images, preset)
            else:
                b64 = await self._generate(prompt, preset)
        return base64.b64decode(_DATA_URI.sub("", b64).strip())

    async def _generate(self, prompt: str, preset: Preset) -> str:
        payload: dict = {
            "model": self.settings.model,
            "prompt": prompt,
            "n": 1,
            "size": preset.size,
            "quality": preset.quality,
            "output_format": self.settings.output_format,
            "moderation": self.settings.moderation,
        }
        if self.settings.output_format in ("jpeg", "webp"):
            payload["output_compression"] = self.settings.output_compression
        return await self._request(
            lambda: self.client.post("/images/generations", json=payload)
        )

    async def _edit(
        self, prompt: str, images: list[bytes], preset: Preset
    ) -> str:
        data = {
            "model": self.settings.model,
            "prompt": prompt,
            "n": "1",
            "size": preset.size,
            "quality": preset.quality,
            "output_format": self.settings.output_format,
            "moderation": self.settings.moderation,
        }
        if self.settings.output_format in ("jpeg", "webp"):
            data["output_compression"] = str(self.settings.output_compression)
        files = [
            ("image[]", (f"input_{i}.png", img, "image/png"))
            for i, img in enumerate(images)
        ]
        return await self._request(
            lambda: self.client.post("/images/edits", data=data, files=files)
        )

    async def _request(self, factory) -> str:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                resp = await factory()
            except httpx.HTTPError as e:
                last_error = e
                if attempt < self.settings.max_retries:
                    await asyncio.sleep(min(1.5 * (attempt + 1), 5.0))
                    continue
                break
            if resp.status_code == 429:
                raise ImageGenError("画图接口当前限流，稍后再试")
            if resp.status_code in (400, 403):
                message = self._error_text(resp)
                if any(k in message.lower() for k in _REJECT_KEYS):
                    raise ContentRejected("这个描述可能触发了内容安全限制，换个说法试试")
                raise ImageGenError(f"生成失败：{message}")
            if 500 <= resp.status_code < 600 and attempt < self.settings.max_retries:
                await asyncio.sleep(min(1.5 * (attempt + 1), 5.0))
                continue
            if resp.status_code >= 400:
                raise ImageGenError(f"生成失败：{self._error_text(resp)}")
            body = resp.json()
            data = body.get("data") or []
            if not data or not data[0].get("b64_json"):
                raise ImageGenError("画图接口没有返回图片数据")
            return data[0]["b64_json"]
        raise ImageGenError(f"画图接口连不上：{last_error!r}")

    @staticmethod
    def _error_text(resp: httpx.Response) -> str:
        try:
            error = (resp.json() or {}).get("error") or {}
            return str(error.get("message") or resp.text[:200])
        except Exception:
            return resp.text[:200]

    # ---- 指令/工具共用入口 ----------------------------------------------

    async def draw_for(
        self,
        incoming: IncomingMessage,
        channel: Channel,
        prompt: str,
        preset: Preset,
        *,
        skip_cooldown: bool = False,
    ) -> str:
        """执行一次绘图并把结果发进会话，返回给调用方的结果描述。

        校验失败/生成失败时不发图，返回原因文本。
        """
        prompt = prompt.strip()
        if not prompt:
            return "请给出要画什么的描述"
        if len(prompt) > self.settings.max_prompt_length:
            return f"描述太长了，请控制在 {self.settings.max_prompt_length} 字以内"
        if not skip_cooldown and (remain := self.cooldown_remaining(incoming.user_id)):
            return f"画图冷却中，{remain} 秒后再试"
        input_images: list[bytes] = []
        if incoming.images:
            try:
                input_images = await self.fetch_input_images(incoming.images)
            except Exception:
                logger.exception("绘图输入图下载失败")
                return "图片读取失败，稍后再试试"
        try:
            png = await self.generate(prompt, preset, input_images)
        except ImageGenError as e:
            return str(e)
        except Exception:
            logger.exception("绘图失败")
            return "画图失败了，稍后再试试"
        try:
            await channel.send_image(incoming.session, to_onebot_file(png))
        except NotImplementedError:
            return "当前聊天渠道不支持发送图片"
        mode = "改图" if input_images else "生图"
        return f"已完成{mode}并发送（{preset.name} {preset.size}）"


def build_imagegen_tool(
    service: ImageGenService, gate: "PermissionGate"
) -> Tool:
    @tool(
        name="draw_image",
        description=(
            "用 AI 画图并把结果图片发到当前聊天。用户想让你画点什么时调用；"
            "prompt 用中文或英文详细描述画面。用户消息里带了图片时会自动"
            "作为底图改图。orientation 可选 square/portrait/landscape，"
            "high_quality 仅在用户明确要求高清时为 true。"
            "发送成功后简短回应即可；返回失败原因时如实转告。"
        ),
    )
    async def draw_image(
        ctx: ToolContext,
        prompt: str,
        orientation: str = "square",
        high_quality: bool = False,
    ) -> str:
        if high_quality:
            preset = PRESETS["高清"]
        elif orientation == "portrait":
            preset = PRESETS["竖图"]
        elif orientation == "landscape":
            preset = PRESETS["横图"]
        else:
            preset = PRESETS["默认"]
        return await service.draw_for(
            ctx.incoming, ctx.channel, prompt, preset,
            skip_cooldown=gate.is_superuser(ctx.incoming.user_id),
        )

    return draw_image


def build_imagegen_commands(service: "ImageGenService | None", gate) -> list:
    """「画图/生图」指令；service 为 None 时回未配置提示"""
    from .commands import Command, prefix

    async def draw_cmd(incoming, channel, rest: str) -> bool:
        session = incoming.session
        if service is None:
            await channel.send_text(
                session, "未配置绘图接口（config.yaml 的 imagegen 段）"
            )
            return True
        if rest in ("", "帮助"):
            await channel.send_text(session, HELP_TEXT)
            return True
        preset, prompt = service.parse_preset(rest)
        # 冷却先于「正在画图」提示判定，占用配额也发生在此刻
        is_su = gate.is_superuser(incoming.user_id)
        if not is_su and (remain := service.cooldown_remaining(incoming.user_id)):
            await channel.send_text(session, f"画图冷却中，{remain} 秒后再试")
            return True
        mode = "改图" if incoming.images else "画图"
        await channel.send_text(session, f"收到！正在{mode}，稍等一会…")
        result = await service.draw_for(
            incoming, channel, prompt, preset, skip_cooldown=True
        )
        if not result.startswith("已完成"):
            await channel.send_text(session, result)
        return True

    return [Command(prefix("画图", "生图"), draw_cmd)]
