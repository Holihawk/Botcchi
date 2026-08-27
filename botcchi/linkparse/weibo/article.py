"""微博头条文章（ttarticle / card.weibo.com/article）：只做摘要卡。

文章详情接口按文章类型返回不同结构，作者/正文字段并非总有，取不到就留空。
"""

import httpx

from ..base import ParsedLink, ParseError, truncate
from . import auth

DETAIL_API = "https://card.weibo.com/article/m/aj/detail"


async def parse_article(client: httpx.AsyncClient, aid: str) -> ParsedLink:
    hdr = await auth.headers(client)
    resp = await client.get(DETAIL_API, params={"id": aid}, headers=hdr)
    if not resp.headers.get("content-type", "").startswith("application/json"):
        raise ParseError("微博文章不存在或已被删除")
    data = resp.json().get("data")
    if not isinstance(data, dict) or not data.get("title"):
        raise ParseError("微博文章内容为空")
    user = data.get("userinfo") or {}
    read = data.get("read_count")
    return ParsedLink(
        platform="微博文章",
        title=data.get("title", ""),
        author=user.get("screen_name", ""),
        stats=f"阅读 {read}" if read else "",
        extra=truncate(data.get("summary") or data.get("introduction") or "", 120),
        url=data.get("target_url") or f"https://card.weibo.com/article/m/show/id/{aid}",
        cover=_cover(data),
        cover_referer="https://weibo.com/",
    )


def _cover(data: dict) -> str | None:
    cover = data.get("cover_img")
    if isinstance(cover, dict):
        return (cover.get("image") or {}).get("url") or None
    return cover or None
