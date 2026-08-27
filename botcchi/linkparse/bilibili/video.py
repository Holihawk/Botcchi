"""稿件视频：view 接口取信息，playurl 取免登录的 html5 直链。"""

import httpx
from loguru import logger

from ..base import (
    Media,
    ParsedLink,
    format_duration,
    format_num,
    truncate,
)
from . import api

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
PLAY_API = "https://api.bilibili.com/x/player/playurl"
REFERER = {"Referer": "https://www.bilibili.com/"}


async def parse_video(
    client: httpx.AsyncClient,
    *,
    bvid: str | None = None,
    avid: str | None = None,
    page: int = 1,
) -> ParsedLink:
    """解析视频；page 为分P页码（从 1 起，越界则取末页）。"""
    data = await api.call(
        client,
        VIEW_API,
        {"bvid": bvid} if bvid else {"aid": avid},
        # 视频 view 接口也会对无设备指纹的请求返回 412；与动态接口保持一致。
        buvid=True,
        referer=(f"https://www.bilibili.com/video/{bvid}" if bvid else "https://www.bilibili.com/"),
    )
    bvid = data.get("bvid", "")
    stat = data.get("stat") or {}
    pages = data.get("pages") or []

    title = data.get("title", "")
    duration = int(data.get("duration") or 0)
    cid = data.get("cid")
    cover = data.get("pic") or None
    index = 0
    if len(pages) > 1:
        index = min(max(page, 1), len(pages)) - 1
        part = pages[index]
        # 分P 名常把正片标题整段重复一遍，只留下真正区分分P的那截
        name = part.get("part", "").replace(title, "").strip(" -|·")
        title = f"{title} | P{index + 1} {name}".rstrip()
        duration = int(part.get("duration") or duration)
        cid = part.get("cid") or cid
        cover = part.get("first_frame") or cover

    url = f"https://www.bilibili.com/video/{bvid}"
    if index:
        url += f"?p={index + 1}"
    return ParsedLink(
        platform="B站",
        title=title,
        author=(data.get("owner") or {}).get("name", ""),
        author_label="UP主",
        duration=format_duration(duration),
        stats=(
            f"播放 {format_num(stat.get('view', 0))} · "
            f"弹幕 {format_num(stat.get('danmaku', 0))} · "
            f"点赞 {format_num(stat.get('like', 0))} · "
            f"收藏 {format_num(stat.get('favorite', 0))}"
        ),
        extra=truncate(data.get("desc", "")),
        url=url,
        cover=cover,
        media=await _fetch_stream(client, bvid, cid, duration),
    )


async def _fetch_stream(
    client: httpx.AsyncClient, bvid: str, cid: int | None, duration: int
) -> Media | None:
    """取无需登录的 html5 直链 mp4（约 480P）；拿不到就只发摘要卡。"""
    if not cid:
        return None
    # 短视频取高清（720P，约 14MB/分钟），长视频降 360P（约 3MB/分钟）控制体积
    high_quality = 1 if duration <= 120 else 0
    try:
        resp = await client.get(
            PLAY_API,
            params={
                "bvid": bvid,
                "cid": cid,
                "qn": 64,
                "platform": "html5",
                "high_quality": high_quality,
            },
            headers=REFERER,
        )
        body = resp.json()
        durl = (body.get("data") or {}).get("durl") or []
        if body.get("code") != 0 or not durl:
            return None
        return Media(
            kind="video",
            url=durl[0]["url"],
            headers=REFERER,
            duration=duration,
            size=int(durl[0].get("size", 0)),
        )
    except Exception:  # 视频流失败不影响摘要卡
        logger.warning(f"B站视频流获取失败: {bvid}")
        return None
