"""小红书解析：xhslink 短链、explore/discovery 笔记页。

免登录但依赖分享链接自带的 xsec_token；数据取页面内嵌
__INITIAL_STATE__ JSON（含 undefined 需替换）。
"""

import json
import re

import httpx

from .base import IOS_UA, Media, ParsedLink, ParseError, Pattern, format_num, truncate

_SHORT = re.compile(r"xhslink\.com/[A-Za-z0-9._?%&+=/#@\-]+")
_NOTE = re.compile(
    r"xiaohongshu\.com/(?:explore|discovery/item|search_result)/"
    r"(?P<note_id>[0-9a-zA-Z]+)\?[^\s]*?xsec_token=(?P<token>[^&\s]+)"
)
_STATE = re.compile(r"window\.__INITIAL_STATE__=(.*?)</script>", re.DOTALL)

PATTERNS: list[Pattern] = [
    ("xhslink.com", _SHORT),
    ("xiaohongshu.com", _NOTE),
]

_HEADERS = {
    "User-Agent": IOS_UA,
    "origin": "https://www.xiaohongshu.com",
}


def _video_url(video: dict) -> str | None:
    """主视频直链：优先 originVideoKey 拼 CDN，退回流地址 masterUrl。"""
    if key := (video.get("consumer") or {}).get("originVideoKey"):
        return f"https://sns-video-bd.xhscdn.com/{key}"
    stream = ((video.get("media") or {}).get("stream")) or {}
    for codec in ("h264", "h265", "h266", "av1"):
        for item in stream.get(codec) or []:
            if url := item.get("masterUrl"):
                return url
    return None


async def parse(client: httpx.AsyncClient, match: re.Match[str]) -> ParsedLink:
    text = match.group(0)
    if _SHORT.fullmatch(text):
        resp = await client.get(
            f"https://{text}", headers=_HEADERS, follow_redirects=True
        )
        match = _NOTE.search(str(resp.url))  # type: ignore[assignment]
        if match is None:
            raise ParseError(f"小红书短链未指向笔记（可能已过期）: {resp.url}")
    note_id, token = match.group("note_id"), match.group("token")

    resp = await client.get(
        f"https://www.xiaohongshu.com/discovery/item/{note_id}"
        f"?xsec_token={token}&xsec_source=pc_share",
        headers=_HEADERS,
        follow_redirects=True,
    )
    resp.raise_for_status()
    # token 失效/无效会被重定向到 /404/ 安全页，页面里仍有 __INITIAL_STATE__ 但无笔记
    if "/404/" in str(resp.url):
        raise ParseError("小红书链接失效或 xsec_token 无效/过期，请重新分享")
    state = _STATE.search(resp.text)
    if state is None:
        raise ParseError("小红书页面无 __INITIAL_STATE__，链接失效或内容已删除")
    raw = state.group(1).replace("undefined", "null")
    note = (
        ((json.loads(raw).get("noteData") or {}).get("data") or {}).get("noteData")
        or {}
    )
    if not note:
        raise ParseError("小红书笔记数据为空（链接可能失效或需要有效 token）")

    interact = note.get("interactInfo") or {}
    cover_id = (note.get("cover") or {}).get("fileId", "")
    cover = (
        f"https://ci.xiaohongshu.com/{cover_id}?imageView2/2/w/1080/format/jpg"
        if cover_id
        else None
    )
    media = None
    duration = ""
    if video := note.get("video"):
        secs = int((video.get("capa") or {}).get("duration") or 0)
        if url := _video_url(video):
            from .base import format_duration

            duration = format_duration(secs)
            media = Media(kind="video", url=url, headers=_HEADERS, duration=secs)
    elif images := note.get("imageList"):
        media = Media(
            kind="images",
            urls=[
                f"https://ci.xiaohongshu.com/{fid}?imageView2/2/w/1080/format/jpg"
                for img in images
                if (fid := img.get("fileId"))
            ],
            headers=_HEADERS,
        )

    def _count(key: str) -> str:
        value = str(interact.get(key) or "0")
        return format_num(int(value)) if value.isdigit() else value

    kind = "视频" if note.get("video") else f"图文（{len(note.get('imageList') or [])} 图）"
    return ParsedLink(
        platform=f"小红书{kind}",
        title=truncate(note.get("title") or note.get("desc") or "（无标题）", 60),
        author=(note.get("user") or {}).get("nickName", ""),
        duration=duration,
        stats=(
            f"点赞 {_count('likedCount')} · 收藏 {_count('collectedCount')} · "
            f"评论 {_count('commentCount')}"
        ),
        extra=truncate(note.get("desc", ""), 80),
        url=f"https://www.xiaohongshu.com/discovery/item/{note_id}",
        cover=cover,
        media=media,
        cache_key=f"xhs:{note_id}",
    )
