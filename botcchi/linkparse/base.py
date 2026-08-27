"""链接解析的公共类型与格式化工具。"""

import re
from dataclasses import dataclass, field
from typing import Literal

PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
IOS_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.6 Mobile/15E148 Safari/604.1"
)


ContentBlock = tuple[Literal["text", "image"], str]
"""图文正文的一个有序块：("text", 段落文本) 或 ("image", 图片URL)"""


class ParseError(Exception):
    """解析失败（资源被删、风控、格式变化等），消息不应因此中断。"""


@dataclass(slots=True)
class Media:
    """解析出的可转发媒体：视频/音频直链、m3u8 流或图集。"""

    kind: Literal["video", "audio", "m3u8", "images"]
    url: str = ""
    urls: list[str] = field(default_factory=list)
    """images：图集各图直链"""
    headers: dict[str, str] = field(default_factory=dict)
    """下载所需请求头（Referer 等）"""
    duration: int = 0
    """秒；0 = 未知"""
    size: int = 0
    """字节；0 = 未知（下载中限额判断）"""


@dataclass(slots=True)
class ParsedLink:
    """解析出的分享内容摘要（发卡片用，不做资源下载）。"""

    platform: str
    title: str
    author: str = ""
    author_label: str = "作者"
    duration: str = ""
    stats: str = ""
    extra: str = ""
    """简介等附加文本（已截断）"""
    url: str = ""
    """规范化后的原内容链接"""
    cover: str | None = None
    cover_referer: str | None = None
    """部分平台的封面 CDN 校验 Referer"""
    media: Media | None = None
    """可转发的音视频/图集；None = 仅摘要卡"""
    content: list[ContentBlock] = field(default_factory=list)
    """图文完整正文的有序块（文字/图片交替），非空则正文走合并转发逐段呈现，
    不再截断。标题/作者/数据仍照常直接发。"""
    content_headers: dict[str, str] = field(default_factory=dict)
    """content 中图片下载所需请求头（Referer 等）"""
    cache_key: str = ""
    """同内容去重键（冷却用），默认取 url"""

    def __post_init__(self) -> None:
        if not self.cache_key:
            self.cache_key = self.url


Pattern = tuple[str, re.Pattern[str]]
"""(关键词, 正则)：先做子串预筛再跑正则，避免每条消息全量正则"""


def format_num(n: int) -> str:
    """12345 → 1.2万，与站内展示习惯一致。"""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}".rstrip("0").rstrip(".") + "亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}".rstrip("0").rstrip(".") + "万"
    return str(n)


def format_duration(seconds: int) -> str:
    m, s = divmod(max(0, seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def truncate(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
