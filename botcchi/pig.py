"""今日小猪占卜与 PigHub 找猪（沿旧版 nonebot-plugin-rollpig）。

- 「今日小猪」：从 62 只本地小猪人格中抽取今天属于你的小猪，出粉色占卜
  卡片。抽取按 (日期, 用户) 确定性求值——当天怎么抽都是同一只，无需存储，
  重启也不变；卡片内容只取决于猪本身，天然命中出图缓存。
- 「随机小猪 [数量]」/「找猪 <关键词>」：从 PigHub（pighub.top）拉取真猪
  照片，多张走合并转发（channel 不支持时退化为逐条、封顶 5 条）。
"""

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from .channels.base import Channel, IncomingMessage
from .render import RenderCache, SvgRenderer, esc, image_data_uri, to_onebot_file
from .social.card import (
    F_CR105,
    F_JCYT,
    F_RXK,
    FONT_DIR,
    PINK,
    _defs,
    _glass,
    _text,
    _wrap,
)
from .tools.registry import Tool, ToolContext, tool

ASSETS = Path(__file__).parent / "assets" / "pig"
PIGHUB_API = "https://pighub.top/api/all-images"
PIGHUB_DATA = "https://pighub.top/data/"
HUB_TTL = 6 * 3600
FORWARD_MAX = 20
SEQUENTIAL_MAX = 5
TZ = ZoneInfo("Asia/Shanghai")

SUB = "#C78BA5"
W = 460
STOPS = (
    '<stop offset="0" stop-color="#FFD9E8"/>'
    '<stop offset="0.55" stop-color="#FBE4E4"/>'
    '<stop offset="1" stop-color="#FFF3F7"/>'
)


@dataclass(frozen=True, slots=True)
class Pig:
    id: str
    name: str
    description: str
    analysis: str


def _load_pigs() -> list[Pig]:
    data = json.loads((ASSETS / "pig.json").read_text("utf-8"))
    return [
        Pig(p["id"], p["name"], p["description"], p["analysis"]) for p in data
    ]


PIGS = _load_pigs()


@lru_cache(maxsize=8)
def _pig_image_uri(pig_id: str) -> str | None:
    for ext in ("png", "jpg", "jpeg", "webp", "gif"):
        file = ASSETS / "image" / f"{pig_id}.{ext}"
        if file.exists():
            return image_data_uri(file.read_bytes())
    return None


def build_pig_card(pig: Pig) -> str:
    lines = _wrap(pig.analysis, 15, W - 120, max_lines=8)
    h = 348 + len(lines) * 24 + 30
    parts = [
        f'<rect width="{W}" height="{h}" fill="url(#bg-grad)"/>',
        _glass(18, 18, W - 36, h - 36, 22, 0.42, 0.75, shadow=True),
        _text(W / 2, 62, 22, F_JCYT, "· 今日小猪占卜 ·", SUB, anchor="middle"),
    ]
    if uri := _pig_image_uri(pig.id):
        parts.append(
            f'<image x="{W / 2 - 80}" y="82" width="160" height="160"'
            f' href="{uri}" preserveAspectRatio="xMidYMid meet"/>'
        )
    parts += [
        _text(W / 2, 290, 30, F_CR105, pig.name, PINK, anchor="middle"),
        _text(W / 2, 320, 15, F_RXK, esc(pig.description), SUB,
              anchor="middle"),
        f'<line x1="60" y1="340" x2="{W - 60}" y2="340" stroke="#F3D7DF"'
        f' stroke-width="1.5"/>',
    ]
    for i, line in enumerate(lines):
        parts.append(
            _text(W / 2, 372 + i * 24, 15, F_JCYT, line, "#A5687C",
                  anchor="middle")
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}"'
        f' viewBox="0 0 {W} {h}" font-family="sans-serif">'
        + _defs(STOPS)
        + "".join(parts)
        + "</svg>"
    )


class PigService:
    def __init__(
        self,
        renderer: SvgRenderer,
        cache: RenderCache | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        settings = renderer.settings.model_copy(deep=True)
        settings.font_dirs = [*settings.font_dirs, str(FONT_DIR)]
        self.renderer = SvgRenderer(settings)
        self.cache = cache
        self.client = client or httpx.AsyncClient(timeout=20)
        self._hub: list[dict] = []
        self._hub_at: float = 0.0

    async def aclose(self) -> None:
        await self.client.aclose()

    # ---- 今日小猪 ----------------------------------------------------------

    @staticmethod
    def daily_pig(user_id: str) -> Pig:
        """按 (日期, 用户) 确定性抽取：当天重复抽结果不变，无需落库"""
        day = datetime.now(TZ).date().isoformat()
        return random.Random(f"{day}:{user_id}").choice(PIGS)

    async def daily_card(self, user_id: str) -> bytes:
        svg = build_pig_card(self.daily_pig(user_id))
        if self.cache is not None:
            return await self.cache.render(self.renderer, svg)
        return await self.renderer.render(svg)

    @staticmethod
    def daily_text(pig: Pig) -> str:
        return (
            f"🐷 今日小猪占卜\n你今天是：{pig.name}（{pig.description}）\n"
            f"{pig.analysis}"
        )

    # ---- PigHub ------------------------------------------------------------

    async def hub_images(self) -> list[dict]:
        now = time.monotonic()
        if self._hub and now - self._hub_at < HUB_TTL:
            return self._hub
        try:
            resp = await self.client.get(PIGHUB_API)
            resp.raise_for_status()
            images = resp.json().get("images") or []
        except Exception as e:
            logger.warning(f"PigHub 获取失败: {e}")
            return self._hub  # 旧缓存兜底（可能为空）
        if images:
            self._hub = images
            self._hub_at = now
        return self._hub

    @staticmethod
    def _hub_url(pig: dict) -> str:
        return PIGHUB_DATA + str(pig.get("thumbnail", "")).split("/")[-1]

    async def _send_pigs(
        self, incoming: IncomingMessage, channel: Channel, pigs: list[dict]
    ) -> None:
        if len(pigs) == 1:
            pig = pigs[0]
            await channel.send_text(incoming.session, str(pig.get("title", "")))
            await channel.send_image(incoming.session, self._hub_url(pig))
            return
        nodes = [
            (str(p.get("title", "猪猪")), str(p.get("title", "")),
             self._hub_url(p))
            for p in pigs
        ]
        try:
            await channel.send_forward(incoming.session, nodes)
        except NotImplementedError:
            for name, _, url in nodes[:SEQUENTIAL_MAX]:
                await channel.send_text(incoming.session, name)
                await channel.send_image(incoming.session, url)

    # ---- 指令 --------------------------------------------------------------

    async def handle_command(
        self, incoming: IncomingMessage, channel: Channel
    ) -> bool:
        text = incoming.text.strip()

        if text in ("今日小猪", "本日小猪", "当日小猪", "今天是什么小猪"):
            pig = self.daily_pig(incoming.user_id)
            try:
                png = await self.daily_card(incoming.user_id)
                await channel.send_image(
                    incoming.session, to_onebot_file(png)
                )
            except Exception:
                logger.exception("小猪卡片渲染失败，退化为文本")
                await channel.send_text(incoming.session, self.daily_text(pig))
            return True

        if text.startswith("随机小猪"):
            rest = text.removeprefix("随机小猪").strip()
            count = int(rest) if rest.isdigit() else 1
            count = max(1, min(count, FORWARD_MAX))
            images = await self.hub_images()
            if not images:
                await channel.send_text(incoming.session, "猪圈空荡荡…稍后再试吧")
                return True
            picks = [random.choice(images) for _ in range(count)]
            await self._send_pigs(incoming, channel, picks)
            return True

        if text.startswith(("找猪", "搜猪")):
            keyword = text[2:].strip()
            if not keyword:
                await channel.send_text(
                    incoming.session, "要找什么猪？用法：找猪 关键词"
                )
                return True
            images = await self.hub_images()
            if not images:
                await channel.send_text(incoming.session, "猪圈空荡荡…稍后再试吧")
                return True
            found = [
                p
                for p in images
                if keyword.lower() in str(p.get("title", "")).lower()
            ]
            if not found:
                await channel.send_text(
                    incoming.session, "你要找的猪仔离家出走了~"
                )
                return True
            await self._send_pigs(incoming, channel, found[:FORWARD_MAX])
            return True

        return False


def build_pig_commands(pig: PigService) -> list:
    """小猪指令注册（匹配交给 handle_command 内部的精确判定）"""
    from .commands import Command, prefix

    async def handle(incoming, channel, rest: str) -> bool:
        return await pig.handle_command(incoming, channel)

    return [
        Command(
            prefix(
                "今日小猪", "本日小猪", "当日小猪", "今天是什么小猪",
                "随机小猪", "找猪", "搜猪",
            ),
            handle,
        )
    ]


# ---- LLM 工具：占卜与找猪也能用自然语言触发 --------------------------------

TOOL_PIG_MAX = 9
"""工具单次最多发几张猪猪图（比指令上限收紧，防模型刷屏）"""


def build_pig_tools(pig: PigService) -> list[Tool]:
    @tool(
        name="daily_pig_fortune",
        description=(
            "抽取当前对话用户的「今日小猪」占卜结果，并把占卜卡片图发到聊天中。"
            "用户想占卜、问自己今天是什么小猪时调用。结果按天固定，"
            "当天重复调用也是同一只。调用后用你的语气简短点评即可，"
            "不必复述卡片全文。"
        ),
    )
    async def daily_pig_fortune(ctx: ToolContext) -> str:
        p = pig.daily_pig(ctx.incoming.user_id)
        note = "占卜卡片已发送。"
        try:
            png = await pig.daily_card(ctx.incoming.user_id)
            await ctx.channel.send_image(
                ctx.incoming.session, to_onebot_file(png)
            )
        except Exception:
            logger.exception("小猪卡片发送失败")
            note = "卡片发送失败了，请把结果用文字告诉用户。"
        return (
            f"用户的今日小猪：{p.name}（{p.description}）。{p.analysis}{note}"
        )

    @tool(
        name="find_pig",
        description=(
            "从 PigHub 猪猪图库发送真猪照片到聊天中。keyword 留空则随机；"
            "填关键词（如「开心」「飞」）按标题搜索。count 为张数（1~9），"
            "多张会打包成合并转发。用户想看猪猪图/云吸猪时调用。"
        ),
    )
    async def find_pig(
        ctx: ToolContext, keyword: str = "", count: int = 1
    ) -> str:
        images = await pig.hub_images()
        if not images:
            return "猪圈空荡荡（PigHub 图库暂时拉不到），请安抚用户稍后再试"
        count = max(1, min(count, TOOL_PIG_MAX))
        keyword = keyword.strip()
        if keyword:
            picks = [
                p
                for p in images
                if keyword.lower() in str(p.get("title", "")).lower()
            ][:count]
            if not picks:
                return f"没有找到标题含「{keyword}」的猪猪，可以让用户换个关键词"
        else:
            picks = [random.choice(images) for _ in range(count)]
        await pig._send_pigs(ctx.incoming, ctx.channel, picks)
        return f"已发送 {len(picks)} 张猪猪照片"

    return [daily_pig_fortune, find_pig]
