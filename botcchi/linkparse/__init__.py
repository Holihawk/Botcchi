"""群聊链接自动解析：识别消息里的分享链接，回复内容摘要卡。

设计参照 nonebot-plugin-parser-lite 的关键词预筛 + 正则匹配思路，但只做
信息卡（标题/作者/封面/数据），不下载音视频。全部为国内资源，
client 统一 trust_env=False 免受本地代理状态影响。
"""

import re
import time
from collections.abc import Awaitable, Callable

import httpx
from loguru import logger

from ..channels.base import Channel, IncomingMessage, SessionKey
from ..config import LinkParseSettings
from ..render import to_onebot_file
from ..tools.registry import Tool, ToolContext, tool
from . import (
    acfun,
    bilibili,
    douyin,
    kuaishou,
    netease,
    rednote,
    tieba,
    weibo,
    x,
    zhihu,
)
from .base import PC_UA, Media, ParsedLink, ParseError, Pattern
from .media import download_capped, fetch_m3u8

__all__ = ["LinkParseService", "ParseError", "ParsedLink", "build_parse_tool"]

Parser = Callable[[httpx.AsyncClient, re.Match[str]], Awaitable[ParsedLink]]

COOLDOWN_SECONDS = 300.0
"""同会话同内容的解析冷却，防多人复读刷屏"""


class LinkParseService:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        settings: LinkParseSettings | None = None,
    ):
        self.client = client or httpx.AsyncClient(
            trust_env=False,
            timeout=10.0,
            headers={"User-Agent": PC_UA},
        )
        self.settings = settings or LinkParseSettings()
        self.parsers: list[tuple[list[Pattern], Parser]] = [
            (bilibili.PATTERNS, bilibili.parse),
            (douyin.PATTERNS, douyin.parse),
            (weibo.PATTERNS, weibo.parse),
            (rednote.PATTERNS, rednote.parse),
            (kuaishou.PATTERNS, kuaishou.parse),
            (x.PATTERNS, x.parse),
            (zhihu.PATTERNS, zhihu.parse),
            (tieba.PATTERNS, tieba.parse),
            (acfun.PATTERNS, acfun.parse),
            (netease.PATTERNS, netease.parse),
        ]
        self._seen: dict[tuple[str, str], float] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    def find(self, text: str) -> tuple[Parser, re.Match[str]] | None:
        """在文本中寻找首个受支持的链接。"""
        text = text.strip()
        if not text:
            return None
        for patterns, parser in self.parsers:
            for keyword, pattern in patterns:
                if keyword not in text:
                    continue
                if match := pattern.search(text):
                    return parser, match
        return None

    async def parse_text(self, text: str) -> ParsedLink | None:
        """解析文本中的链接；无匹配返回 None，匹配但失败抛 ParseError """
        found = self.find(text)
        if found is None:
            return None
        parser, match = found
        try:
            return await parser(self.client, match)
        except ParseError:
            raise
        except Exception as e:  # 网络/格式异常统一归为解析失败
            raise ParseError(str(e)) from e

    def _in_cooldown(self, session: SessionKey, key: str) -> bool:
        now = time.monotonic()
        self._seen = {k: v for k, v in self._seen.items() if v > now}
        if self._seen.get((str(session), key), 0) > now:
            return True
        self._seen[(str(session), key)] = now + COOLDOWN_SECONDS
        return False

    def reply_text(self, link: ParsedLink, *, with_extra: bool = True) -> str:
        head = f"【{link.platform}】{link.title}"
        meta = " ｜ ".join(
            part
            for part in (
                f"{link.author_label}：{link.author}" if link.author else "",
                link.duration,
            )
            if part
        )
        lines = [head]
        if meta:
            lines.append(meta)
        if link.stats:
            lines.append(link.stats)
        if with_extra and link.extra:
            lines.append(link.extra)
        if link.url:
            lines.append(link.url)
        return "\n".join(lines)

    def _media_verdict(self, media: Media | None) -> tuple[Media | None, str]:
        """判定媒体是否可转发，返回 (可发媒体, 超限提示)"""
        if media is None or not self.settings.send_media:
            return None, ""
        if media.kind in ("video", "m3u8", "audio"):
            cap = self.settings.max_video_seconds
            if cap and media.duration > cap:
                return None, "（超出转发时长限制，想看去链接吧）"
            if media.size and media.size > self.settings.max_media_mb * 1024 * 1024:
                return None, "（文件太大就不搬运了，想看去链接吧）"
        return media, ""

    @staticmethod
    def _is_rich(link: ParsedLink) -> bool:
        """正文是否值得走「摘要卡 + 合并转发」：有配图，或正文长到会被截断"""
        if not link.content:
            return False
        images = sum(1 for kind, _ in link.content if kind == "image")
        text_len = sum(len(v) for kind, v in link.content if kind == "text")
        return images > 0 or text_len > 120

    async def send_card(
        self, session: SessionKey, channel: Channel, link: ParsedLink
    ) -> None:
        """封面 + 摘要文本 + （限额内的）音视频/图集。

        图文类（B站图文/长动态等）标题·作者·数据直接发，完整正文走合并转发
        逐段呈现（文字与图片按原文交替），既不截断也不刷屏。
        """
        if self._is_rich(link):
            await self._send_rich(session, channel, link)
            return
        media, note = self._media_verdict(link.media)
        # 即将转发图集时封面就是重复信息，跳过
        if link.cover and (media is None or media.kind != "images"):
            try:
                headers = {"Referer": link.cover_referer} if link.cover_referer else {}
                resp = await self.client.get(link.cover, headers=headers)
                resp.raise_for_status()
                await channel.send_image(session, to_onebot_file(resp.content))
            except NotImplementedError:
                pass
            except Exception:
                logger.warning(f"封面获取/发送失败: {link.cover}")
        text = self.reply_text(link)
        if note:
            text += f"\n{note}"
        await channel.send_text(session, text)
        if media is not None:
            try:
                await self._send_media(session, channel, media)
            except NotImplementedError:
                logger.info(f"channel {channel.name} 不支持该媒体类型")
            except Exception:
                logger.exception(f"媒体转发失败: {link.url}")

    async def _send_rich(
        self, session: SessionKey, channel: Channel, link: ParsedLink
    ) -> None:
        """图文：先发标题/作者/数据摘要，正文再走合并转发逐段呈现"""
        await channel.send_text(session, self.reply_text(link, with_extra=False))
        nodes = await self._content_nodes(link)
        if not nodes:
            return
        try:
            await channel.send_forward(session, nodes)
        except NotImplementedError:
            # 渠道不支持合并转发：退化为逐段直接发（图文交替顺序不变）
            for _name, text, image in nodes:
                if text:
                    await channel.send_text(session, text)
                if image:
                    await channel.send_image(session, image)
        except Exception:
            logger.exception(f"图文合并转发失败: {link.url}")

    async def _content_nodes(
        self, link: ParsedLink
    ) -> list[tuple[str, str, str | None]]:
        """把有序正文块转成合并转发节点。

        图片直接把 URL 交给协议端服务端抓取（同小猪合并转发的做法）——NapCat
        对合并转发节点里的 base64 图片渲染不可靠，会整张卡片失败只剩标题。
        """
        name = link.author or link.platform
        nodes: list[tuple[str, str, str | None]] = []
        images = 0
        for kind, value in link.content:
            if kind == "text":
                if value.strip():
                    nodes.append((name, value.strip(), None))
            elif images < self.settings.max_content_images:
                nodes.append((name, "", value))
                images += 1
        return nodes

    async def _send_media(
        self, session: SessionKey, channel: Channel, media: Media
    ) -> None:
        max_bytes = self.settings.max_media_mb * 1024 * 1024
        if media.kind == "images":
            for url in media.urls[: self.settings.max_images]:
                data = await download_capped(self.client, url, media.headers, max_bytes)
                if data is not None:
                    await channel.send_image(session, to_onebot_file(data))
            return
        if media.kind == "m3u8":
            data = await fetch_m3u8(media.url, media.headers, max_bytes)
        else:
            data = await download_capped(
                self.client, media.url, media.headers, max_bytes
            )
        if data is None:
            logger.info(f"媒体超出大小限制（>{self.settings.max_media_mb}MB），放弃")
            return
        if media.kind == "audio":
            await channel.send_record(session, to_onebot_file(data))
        else:
            await channel.send_video(session, to_onebot_file(data))

    async def handle_message(self, incoming: IncomingMessage, channel: Channel) -> bool:
        """群消息的被动解析入口。返回 True 表示已解析并回复（消费消息）"""
        found = self.find(incoming.text)
        if found is None:
            return False
        parser, match = found
        if self._in_cooldown(incoming.session, match.group(0)):
            return False
        try:
            link = await parser(self.client, match)
        except ParseError as e:
            # 被动场景解析失败保持安静，不打扰群聊
            logger.info(f"链接解析失败（{match.group(0)}）: {e}")
            return False
        await self.send_card(incoming.session, channel, link)
        logger.info(
            f"[{incoming.session}] 链接解析: {match.group(0)} -> "
            f"{link.platform}《{link.title[:30]}》"
        )
        return True


def build_parse_tool(service: LinkParseService) -> Tool:
    @tool(
        name="parse_link",
        description=(
            "解析视频/图文分享链接（支持B站、抖音、微博、小红书、快手、X、"
            "知乎、贴吧、AcFun、网易云），把封面和内容摘要发到当前聊天。用户发来链接让你"
            "看看/解析/是什么时调用，url 传消息里的链接原文（短链也可以）。"
        ),
    )
    async def parse_link(ctx: ToolContext, url: str) -> str:
        try:
            link = await service.parse_text(url)
        except ParseError as e:
            return f"解析失败：{e}。请委婉告诉用户这个链接打不开或暂不支持"
        if link is None:
            return "没有识别出受支持的链接（支持B站、抖音、微博、小红书、快手、X、知乎、贴吧、AcFun、网易云）"
        await service.send_card(ctx.incoming.session, ctx.channel, link)
        summary = service.reply_text(link).replace("\n", "；")
        return f"解析卡片已发送。内容：{summary}。简短回应即可，不要复述卡片"

    return parse_link
