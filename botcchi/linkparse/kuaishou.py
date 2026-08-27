"""快手解析：v.kuaishou / www.kuaishou 短链、chenzhongtech / gifshow 分享页。

分享页内嵌 INIT_STATE JSON，其 key 做了逐字符 +1 的凯撒混淆
（"/rest/…" 变成 "0sftu…"），还原后取 photo 数据。各种入口统一先跟随
重定向拿到落地分享页，再解析，photoId 为字母数字混合（非纯数字）。
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

_SHORT = re.compile(r"v\.kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+")
_WWW = re.compile(r"(?:www\.)?kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+")
# photoId 为字母数字混合，早期版本误设为纯数字导致直链漏解析
_PHOTO = re.compile(
    r"(?:v\.m\.)?(?:m\.gifshow\.com|chenzhongtech\.com)"
    r"/fw/(?:photo|long-video)/(?P<pid>[A-Za-z0-9]+)"
)
_INIT = re.compile(r"window\.INIT_STATE\s*=\s*(.*?)</script>", re.DOTALL)

PATTERNS: list[Pattern] = [
    ("v.kuaishou", _SHORT),
    ("kuaishou.com", _WWW),
    ("chenzhongtech", _PHOTO),
    ("m.gifshow.com", _PHOTO),
]

_HEADERS = {"User-Agent": IOS_UA}
_DECRYPT = str.maketrans(
    "".join(chr(i) for i in range(256)),
    "".join(chr((i - 1) % 256) for i in range(256)),
)
_INFO_KEY = "/rest/wd/ugH5App/photo/simple/info"


async def parse(client: httpx.AsyncClient, match: re.Match[str]) -> ParsedLink:
    # 各入口统一跟随重定向到落地分享页；long-video 归一到 photo
    resp = await client.get(
        f"https://{match.group(0)}", headers=_HEADERS, follow_redirects=True
    )
    html = resp.text
    if _INIT.search(html) is None:
        page_url = str(resp.url).replace("/fw/long-video/", "/fw/photo/")
        resp = await client.get(page_url, headers=_HEADERS, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text
    init = _INIT.search(html)
    if init is None:
        raise ParseError("快手分享页无 INIT_STATE，可能触发风控或作品已删除")

    photo = _extract_photo(json.loads(init.group(1)))
    if not photo:
        raise ParseError("快手数据里没有作品内容")

    media, duration = _media(photo)
    is_video = bool(photo.get("mainMvUrls"))
    return ParsedLink(
        platform=f"快手{'视频' if is_video else '图集'}",
        title=truncate(photo.get("caption", "") or "（无标题）", 60),
        author=str(photo.get("userName", "")).replace("ㅤ", "").strip(),
        duration=duration,
        stats=(
            f"播放 {format_num(int(photo.get('viewCount') or 0))} · "
            f"点赞 {format_num(int(photo.get('likeCount') or 0))} · "
            f"评论 {format_num(int(photo.get('commentCount') or 0))}"
        ),
        url=f"https://m.gifshow.com/fw/photo/{photo.get('photoId', '')}",
        cover=None if not is_video else _cover(photo),
        media=media,
        cache_key=f"kuaishou:{photo.get('photoId', '')}",
    )


def _extract_photo(state: dict) -> dict | None:
    """还原被凯撒混淆的 key，取 photo/simple/info 下的作品。"""
    for key, value in state.items():
        if key.translate(_DECRYPT).startswith(_INFO_KEY) and isinstance(value, dict):
            return (value.get("info") or {}).get("photo")
    return None


def _cover(photo: dict) -> str | None:
    return next(
        (c.get("url") for c in photo.get("coverUrls") or [] if c.get("url")), None
    )


def _media(photo: dict) -> tuple[Media | None, str]:
    if mv := photo.get("mainMvUrls"):
        secs = int(photo.get("duration") or 0) // 1000
        media = Media(kind="video", url=mv[0]["url"], headers=_HEADERS, duration=secs)
        return media, format_duration(secs)
    atlas = (photo.get("ext_params") or {}).get("atlas") or {}
    cdn = next((c.get("cdn") for c in atlas.get("cdnList") or []), "")
    urls = [f"https://{cdn}/{route}" for route in atlas.get("list") or [] if cdn]
    if urls:
        return Media(kind="images", urls=urls, headers=_HEADERS), ""
    return None, ""
