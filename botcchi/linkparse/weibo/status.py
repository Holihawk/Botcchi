"""微博博文：show 接口取信息，长文另取 longtext，图集/视频按 media 转发。"""

import re

import httpx

from ..base import Media, ParsedLink, ParseError, format_num, truncate
from . import auth

SHOW_API = "https://weibo.com/ajax/statuses/show"
LONGTEXT_API = "https://weibo.com/ajax/statuses/longtext"
_REFERER = {"Referer": "https://weibo.com/"}

_TAG = re.compile(r"<br\s*/?>|</p>")
_HTML = re.compile(r"<[^>]+>")
# 长文正文末尾会挂一个 t.cn 展开链接 + 零宽字符，去掉
_TAIL = re.compile(r"\s*https?://t\.cn/\S+​?$")


async def parse_status(client: httpx.AsyncClient, wid: str) -> ParsedLink:
    hdr = await auth.headers(client)
    data = await _show(client, wid, hdr)
    return await _to_link(client, data, hdr)


async def _show(client: httpx.AsyncClient, wid: str, hdr: dict) -> dict:
    resp = await client.get(SHOW_API, params={"id": wid, "locale": "zh-CN"}, headers=hdr)
    if not resp.headers.get("content-type", "").startswith("application/json"):
        raise ParseError("微博不存在或已被删除")
    data = resp.json()
    if not isinstance(data, dict) or not data.get("user"):
        raise ParseError(f"微博接口异常: {str(data)[:80]}")
    return data


async def _to_link(
    client: httpx.AsyncClient, data: dict, hdr: dict, *, repost: bool = False
) -> ParsedLink:
    user = data.get("user") or {}
    text = await _full_text(client, data, hdr)
    images = _images(data)
    video = _video(data)

    extra = text
    rt = data.get("retweeted_status")
    if rt and not repost:
        rt_user = (rt.get("user") or {}).get("screen_name", "")
        rt_text = truncate(_clean(rt.get("text_raw", "")), 50) or "微博"
        line = f"转发 @{rt_user}：{rt_text}"
        extra = f"{text}\n{line}" if text else line
        # 转发且本体无媒体时，继承被转微博的图/视频
        if not images and not video:
            images, video = _images(rt), _video(rt)

    media = Media(kind="images", urls=images, headers=_REFERER) if images else video
    page_pic = ((data.get("page_info") or {}).get("page_pic")) or None
    mblogid = data.get("mblogid") or data.get("idstr", "")
    return ParsedLink(
        platform=f"微博（{len(images)}图）" if images else "微博",
        title=truncate(text or (rt and "转发微博") or "微博", 100),
        author=user.get("screen_name", ""),
        stats=(
            f"转发 {format_num(int(data.get('reposts_count') or 0))} · "
            f"评论 {format_num(int(data.get('comments_count') or 0))} · "
            f"点赞 {format_num(int(data.get('attitudes_count') or 0))}"
        ),
        extra=truncate(extra, 120),
        url=f"https://weibo.com/{user.get('idstr', '')}/{mblogid}",
        # 图集会整组转发，封面就不再单发；视频用 page_pic 兜底
        cover=None if images else page_pic,
        cover_referer="https://weibo.com/",
        media=media,
        cache_key=f"weibo:{mblogid}",
    )


async def _full_text(client: httpx.AsyncClient, data: dict, hdr: dict) -> str:
    if data.get("isLongText"):
        try:
            resp = await client.get(
                LONGTEXT_API, params={"id": data.get("mblogid")}, headers=hdr
            )
            body = (resp.json().get("data") or {}) if resp.status_code == 200 else {}
            if full := body.get("longTextContent_raw") or body.get("longTextContent"):
                return _clean(full)
        except Exception:  # 长文失败退回截断正文
            pass
    return _clean(data.get("text_raw", ""))


def _clean(text: str) -> str:
    text = _HTML.sub("", _TAG.sub("\n", text))
    return _TAIL.sub("", text).strip()


def _images(data: dict) -> list[str]:
    infos = data.get("pic_infos") or {}
    order = data.get("pic_ids") or list(infos.keys())
    urls = []
    for pid in order:
        pic = infos.get(pid) or {}
        if url := (pic.get("large") or {}).get("url") or (
            pic.get("original") or {}
        ).get("url"):
            urls.append(url)
    return urls


def _video(data: dict) -> Media | None:
    page = data.get("page_info") or {}
    if page.get("object_type") != "video" and page.get("type") != "video":
        return None
    info = page.get("media_info") or {}
    url = info.get("stream_url_hd") or info.get("stream_url")
    if not url:
        return None
    return Media(
        kind="video",
        url=url,
        headers=_REFERER,
        duration=int(info.get("duration") or 0),
    )
