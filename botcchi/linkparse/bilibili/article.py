"""专栏（cv 号）。

内容详情接口 x/article/view 的免登录配额很紧（动辄 -509），改用只读的
viewinfo：标题、作者、数据、题图都有，够拼一张摘要卡。
"""

import httpx

from ..base import ParsedLink, format_num
from . import api

VIEWINFO_API = "https://api.bilibili.com/x/article/viewinfo"


async def parse_article(client: httpx.AsyncClient, cvid: str) -> ParsedLink:
    data = await api.call(
        client,
        VIEWINFO_API,
        {"id": cvid},
        referer=f"https://www.bilibili.com/read/cv{cvid}",
    )
    stats = data.get("stats") or {}
    images = data.get("origin_image_urls") or []
    return ParsedLink(
        platform="B站专栏",
        title=data.get("title", ""),
        author=data.get("author_name", ""),
        author_label="作者",
        stats=(
            f"阅读 {format_num(int(stats.get('view') or 0))} · "
            f"点赞 {format_num(int(stats.get('like') or 0))} · "
            f"收藏 {format_num(int(stats.get('favorite') or 0))} · "
            f"评论 {format_num(int(stats.get('reply') or 0))}"
        ),
        url=f"https://www.bilibili.com/read/cv{cvid}",
        cover=data.get("banner_url") or (images[0] if images else None),
    )
