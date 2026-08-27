"""抖音解析：v/jx.douyin 短链，video/note/slides/article 链接与精选页。

走 m.douyin.com 分享页内嵌的 _ROUTER_DATA JSON（iOS UA），不依赖浏览器；
视频、图文（含 Live Photo，取静图）、图集 slides 都由同一份 loaderData 覆盖。
图文/风控页偶尔无 _ROUTER_DATA，抛 ParseError 由上层静默放行。
"""

import json
import re

import httpx

from .base import (
    IOS_UA,
    Media,
    ParsedLink,
    ParseError,
    Pattern,
    format_duration,
    format_num,
    truncate,
)

_SHORT = re.compile(r"(?:v|jx)\.douyin\.com/[0-9A-Za-z_\-]+")
# www./m./jingxuan. 前缀，iesdouyin 同源，路径可能是 /share/ 或精选的 /m/
_LONG = re.compile(
    r"(?:www\.|m\.|jingxuan\.)?(?:ies)?douyin\.com/(?:share/|m/)?"
    r"(?P<ty>video|note|slides|article)/(?P<vid>\d+)"
)
_ROUTER = re.compile(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.DOTALL)

PATTERNS: list[Pattern] = [
    ("v.douyin", _SHORT),
    ("jx.douyin", _SHORT),
    ("douyin.com", _LONG),
]

_HEADERS = {"User-Agent": IOS_UA}
_REFERER = {"Referer": "https://www.douyin.com/"}


async def parse(client: httpx.AsyncClient, match: re.Match[str]) -> ParsedLink:
    text = match.group(0)
    if _SHORT.fullmatch(text):
        resp = await client.get(
            f"https://{text}", headers=_HEADERS, follow_redirects=True
        )
        match = _LONG.search(str(resp.url))  # type: ignore[assignment]
        if match is None:
            raise ParseError(f"抖音短链未指向作品: {resp.url}")
    ty, vid = match.group("ty"), match.group("vid")

    item = await _fetch_item(client, ty, vid)
    stats = item.get("statistics") or {}
    images = item.get("images") or []
    video = item.get("video") or {}

    media, duration = _media(images, video)
    is_image = bool(images)
    kind = f"图集（{len(images)}图）" if is_image else "视频"
    return ParsedLink(
        platform=f"抖音{kind}",
        title=truncate(item.get("desc", "") or "（无文案）", 60),
        author=(item.get("author") or {}).get("nickname", ""),
        duration=duration,
        stats=(
            f"点赞 {format_num(stats.get('digg_count', 0))} · "
            f"评论 {format_num(stats.get('comment_count', 0))} · "
            f"收藏 {format_num(stats.get('collect_count', 0))}"
        ),
        url=f"https://www.douyin.com/{'note' if is_image else 'video'}/{vid}",
        # 图集会整组转发，封面不再单发
        cover=None if is_image else _cover(video, images),
        cover_referer="https://www.douyin.com/",
        media=media,
        cache_key=f"douyin:{vid}",
    )


async def _fetch_item(client: httpx.AsyncClient, ty: str, vid: str) -> dict:
    """取分享页的作品数据；video/note/slides 都用同一分享路由"""
    # article 走 video 页；其余（含 slides）保留原类型，让分享页给对应 loaderData
    share_ty = "video" if ty == "article" else ty
    resp = await client.get(
        f"https://m.douyin.com/share/{share_ty}/{vid}",
        headers=_HEADERS,
        follow_redirects=True,
    )
    resp.raise_for_status()
    router = _ROUTER.search(resp.text)
    if router is None:
        raise ParseError("分享页无 _ROUTER_DATA，可能触发风控或作品已删除")
    loader = json.loads(router.group(1).strip()).get("loaderData") or {}
    item = _first_item(loader)
    if item is None:
        raise ParseError("分享数据里没有作品内容")
    return item


def _first_item(loader: dict) -> dict | None:
    """从任意 *（id）/page 的 videoInfoRes.item_list 里取首个作品"""
    for key, page in loader.items():
        if "page" not in key or not isinstance(page, dict):
            continue
        items = (page.get("videoInfoRes") or {}).get("item_list") or []
        if items:
            return items[0]
    return None


def _media(images: list[dict], video: dict) -> tuple[Media | None, str]:
    if images:
        urls = [
            u[0].replace("playwm", "play")
            for img in images
            if (u := img.get("url_list"))
        ]
        return Media(kind="images", urls=urls, headers=_REFERER), ""
    if play_urls := (video.get("play_addr") or {}).get("url_list"):
        seconds = int(video.get("duration", 0)) // 1000
        media = Media(
            kind="video",
            url=play_urls[0].replace("playwm", "play"),
            headers=_REFERER,
            duration=seconds,
        )
        return media, format_duration(seconds)
    return None, ""


def _cover(video: dict, images: list[dict]) -> str | None:
    if cover_urls := (video.get("cover") or {}).get("url_list"):
        return cover_urls[0]
    if images and (image_urls := images[0].get("url_list")):
        return image_urls[0]
    return None
