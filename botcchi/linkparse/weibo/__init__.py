"""微博解析：博文（含长文/图集/视频/转发）与头条文章。

内容接口现在都要访客态（无访客态一律 302 到登录墙），握手逻辑收在 auth 里。
按命中的正则分发到 status / article 子解析器。
"""

import re
from collections.abc import Awaitable, Callable

import httpx

from ..base import ParsedLink, Pattern
from .article import parse_article
from .status import parse_status

__all__ = ["PATTERNS", "parse"]

# 头条文章：ttarticle 的 id 在查询串，card/article 的在路径 /id/
_TTARTICLE = re.compile(r"weibo\.com/ttarticle/\S*?id=(?P<aid>\d+)")
_ARTICLE = re.compile(r"(?:card\.)?weibo\.com/article/\S*?/id/(?P<aid>\d+)")
# 博文：m.weibo.cn/status|detail|{uid}/{wid}、weibo.com/{uid}/{bid}
_STATUS = re.compile(r"(?:m\.)?weibo\.cn/(?:status|detail|\d+)/(?P<wid>[0-9a-zA-Z]+)")
_POST = re.compile(r"weibo\.com/\d+/(?P<wid>[0-9a-zA-Z]+)")

# 文章在前，避免 weibo.com 前缀被博文规则抢
PATTERNS: list[Pattern] = [
    ("ttarticle", _TTARTICLE),
    ("weibo.com/article", _ARTICLE),
    ("weibo.cn", _STATUS),
    ("weibo.com", _POST),
]

Handler = Callable[[httpx.AsyncClient, re.Match[str]], Awaitable[ParsedLink]]

_HANDLERS: dict[re.Pattern[str], Handler] = {
    _TTARTICLE: lambda c, m: parse_article(c, m["aid"]),
    _ARTICLE: lambda c, m: parse_article(c, m["aid"]),
    _STATUS: lambda c, m: parse_status(c, m["wid"]),
    _POST: lambda c, m: parse_status(c, m["wid"]),
}


async def parse(client: httpx.AsyncClient, match: re.Match[str]) -> ParsedLink:
    return await _HANDLERS[match.re](client, match)
