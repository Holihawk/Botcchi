"""收藏夹：取首页 20 条内容做目录卡。"""

import httpx

from ..base import ParsedLink, ParseError, format_num, truncate
from . import api

FAV_API = "https://api.bilibili.com/x/v3/fav/resource/list"


async def parse_favlist(client: httpx.AsyncClient, fav_id: str) -> ParsedLink:
    data = await api.call(
        client,
        FAV_API,
        {
            "media_id": fav_id,
            "pn": 1,
            "ps": 20,
            "order": "mtime",
            "tid": 0,
            "type": 0,
            "platform": "web",
        },
    )
    info = data.get("info") or {}
    if not info.get("title"):
        raise ParseError("收藏夹不存在或未公开")
    upper = info.get("upper") or {}
    cnt = info.get("cnt_info") or {}
    medias = data.get("medias") or []
    return ParsedLink(
        platform="B站收藏夹",
        title=info["title"],
        author=upper.get("name", ""),
        author_label="创建者",
        stats=(
            f"内容 {info.get('media_count', 0)} 个 · "
            f"播放 {format_num(int(cnt.get('play') or 0))} · "
            f"收藏 {format_num(int(cnt.get('collect') or 0))}"
        ),
        extra=truncate(
            "；".join(m.get("title", "") for m in medias[:3]) or info.get("intro", ""),
            100,
        ),
        url=f"https://space.bilibili.com/{upper.get('mid', '')}/favlist?fid={fav_id}",
        cover=info.get("cover") or (medias[0].get("cover") if medias else None),
    )
