"""B站解析：视频（含分P）、动态、图文、专栏、直播间、收藏夹与 b23 短链。

一个平台对应多种内容，靠命中的正则本身分发到子解析器；短链先展开成长链
再按同一张表重新识别。数据全走公开 web 接口，无需登录态。
"""

import re
from collections.abc import Awaitable, Callable

import httpx

from ..base import ParsedLink, ParseError, Pattern
from .article import parse_article
from .dynamic import parse_dynamic
from .favlist import parse_favlist
from .live import parse_live
from .video import parse_video

__all__ = ["PATTERNS", "parse"]

_SHORT = re.compile(r"(?:b23\.tv|bili2233\.cn)/[0-9A-Za-z]+")
_LIVE = re.compile(r"live\.bilibili\.com/(?:h5/)?(?P<room_id>\d+)")
_DYNAMIC = re.compile(
    r"(?:t\.bilibili\.com|bilibili\.com/(?:dynamic|opus))/(?P<dynamic_id>\d+)"
)
_READ = re.compile(r"bilibili\.com/read/(?:cv|mobile/)(?P<cvid>\d+)")
_FAVLIST = re.compile(r"bilibili\.com/\S*?favlist\?fid=(?P<fav_id>\d+)")
_BV = re.compile(r"bilibili\.com(?:/video)?/(?P<bvid>BV[0-9A-Za-z]{10})(?P<tail>\S*)")
_AV = re.compile(r"bilibili\.com(?:/video)?/av(?P<avid>\d{6,})(?P<tail>\S*)")
# 稍后再看、播放列表等把 bvid 放在查询参数里
_BV_PARAM = re.compile(
    r"bilibili\.com/\S*?[?&]bvid=(?P<bvid>BV[0-9A-Za-z]{10})(?P<tail>\S*)"
)
_BARE_BV = re.compile(r"^(?P<bvid>BV[0-9A-Za-z]{10})(?:\s+(?P<p>\d{1,3}))?$")
_BARE_AV = re.compile(r"^av(?P<avid>\d{6,})(?:\s+(?P<p>\d{1,3}))?$")

# 先匹配专项内容，最后才落到视频，避免 bilibili.com 前缀互相抢
PATTERNS: list[Pattern] = [
    ("b23.tv", _SHORT),
    ("bili2233", _SHORT),
    ("bilibili.com", _LIVE),
    ("bilibili.com", _DYNAMIC),
    ("bilibili.com", _READ),
    ("bilibili.com", _FAVLIST),
    ("bilibili.com", _BV),
    ("bilibili.com", _AV),
    ("bilibili.com", _BV_PARAM),
    ("BV", _BARE_BV),
    ("av", _BARE_AV),
]

Handler = Callable[[httpx.AsyncClient, re.Match[str]], Awaitable[ParsedLink]]

_PAGE = re.compile(r"[?&]p=(\d+)")


def _page_of(match: re.Match[str]) -> int:
    """分P页码：链接取 ?p=，裸号取号后跟的数字"""
    groups = match.groupdict()
    if page := groups.get("p"):
        return int(page)
    if found := _PAGE.search(groups.get("tail") or ""):
        return int(found[1])
    return 1


async def _video(client: httpx.AsyncClient, match: re.Match[str]) -> ParsedLink:
    groups = match.groupdict()
    return await parse_video(
        client,
        bvid=groups.get("bvid"),
        avid=groups.get("avid"),
        page=_page_of(match),
    )


_HANDLERS: dict[re.Pattern[str], Handler] = {
    _LIVE: lambda c, m: parse_live(c, m["room_id"]),
    _DYNAMIC: lambda c, m: parse_dynamic(c, m["dynamic_id"]),
    _READ: lambda c, m: parse_article(c, m["cvid"]),
    _FAVLIST: lambda c, m: parse_favlist(c, m["fav_id"]),
    _BV: _video,
    _AV: _video,
    _BV_PARAM: _video,
    _BARE_BV: _video,
    _BARE_AV: _video,
}


async def parse(client: httpx.AsyncClient, match: re.Match[str]) -> ParsedLink:
    if match.re is _SHORT:
        match = await _expand(client, match.group(0))
    return await _HANDLERS[match.re](client, match)


async def _expand(client: httpx.AsyncClient, short: str) -> re.Match[str]:
    """b23 短链可指向任意内容，展开后按长链规则重新识别"""
    resp = await client.get(f"https://{short}", follow_redirects=True)
    final = str(resp.url)
    for pattern in (_LIVE, _DYNAMIC, _READ, _FAVLIST, _BV, _AV, _BV_PARAM):
        if found := pattern.search(final):
            return found
    raise ParseError(f"短链指向了暂不支持的内容: {final}")
