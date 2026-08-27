"""X（推特）解析：x.com / twitter.com 的 status 链接。

官方 GraphQL 需登录态与签名，参照 nonebot-plugin-parser-lite 走第三方中转
easycomment.ai（POST {pid}，无需自前认证/签名）。中转失效时解析静默失败，
只影响该平台。推文媒体在 twimg CDN（境外），能否转发取决于运行网络，
拿不到时只发摘要卡即可。
"""

import html
import re

import httpx

from .base import (
    PC_UA,
    Media,
    ParsedLink,
    ParseError,
    Pattern,
    format_duration,
    format_num,
    truncate,
)

API = "https://easycomment.ai/api/twitter/v1/free/get-tweet-detail"

_STATUS = re.compile(r"(?:x|twitter)\.com/[0-9A-Za-z_]{1,20}/status/(?P<id>\d+)")

PATTERNS: list[Pattern] = [
    ("x.com", _STATUS),
    ("twitter.com", _STATUS),
]

# twimg 媒体多数不校验 Referer，但带上更稳；UA 用桌面
_MEDIA_HEADERS = {"User-Agent": PC_UA, "Referer": "https://x.com/"}
_T_CO = re.compile(r"\s*https?://t\.co/\w+\s*$")


async def parse(client: httpx.AsyncClient, match: re.Match[str]) -> ParsedLink:
    tweet_id = match.group("id")
    resp = await client.post(
        API,
        json={"pid": tweet_id},
        headers={"Content-Type": "application/json", "User-Agent": PC_UA},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 100000:
        raise ParseError(f"X 中转接口错误: {data.get('message') or data.get('code')}")

    tweet_map = _index_tweets(data)
    node = tweet_map.get(tweet_id)
    if node is None:
        raise ParseError("推文不存在、已删除或不可见")

    legacy = node.get("legacy") or {}
    author, screen_name = _author(node)
    media, cover, duration = _media(legacy)

    return ParsedLink(
        platform=f"X{_kind_label(legacy, media)}",
        title=truncate(_text(legacy) or "（无文字）", 80),
        author=author,
        duration=duration,
        stats=_stats(node, legacy),
        extra=_repost_line(node, tweet_id, tweet_map),
        url=f"https://x.com/{screen_name}/status/{tweet_id}",
        cover=cover,
        cover_referer="https://x.com/" if cover else None,
        media=media,
        cache_key=f"x:{tweet_id}",
    )


def _index_tweets(data: dict) -> dict[str, dict]:
    """把时间线里所有推文按 rest_id 建索引（含 TweetWithVisibilityResults）"""
    convo = ((data.get("data") or {}).get("data") or {}).get(
        "threaded_conversation_with_injections_v2"
    ) or {}
    entries = next(
        (
            instr.get("entries") or []
            for instr in convo.get("instructions") or []
            if instr.get("type") == "TimelineAddEntries"
        ),
        [],
    )
    out: dict[str, dict] = {}
    for entry in entries:
        content = entry.get("content") or {}
        if content.get("__typename") != "TimelineTimelineItem":
            continue
        item = content.get("itemContent") or {}
        if item.get("__typename") != "TimelineTweet":
            continue
        node = _node((item.get("tweet_results") or {}).get("result") or {})
        if node and (rid := node.get("rest_id")):
            out[rid] = node
    return out


def _node(result: dict) -> dict | None:
    """展开 Tweet / TweetWithVisibilityResults，返回真实推文节点"""
    typename = result.get("__typename")
    if typename == "Tweet":
        return result
    if typename == "TweetWithVisibilityResults":
        return result.get("tweet")
    return None


def _author(node: dict) -> tuple[str, str]:
    user = ((node.get("core") or {}).get("user_results") or {}).get("result") or {}
    core = user.get("core") or {}
    name = core.get("name") or ""
    screen = core.get("screen_name") or ""
    return (f"{name}（@{screen}）" if name else f"@{screen}"), screen


def _text(legacy: dict) -> str:
    full = legacy.get("full_text") or ""
    rng = legacy.get("display_text_range")
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        try:
            full = full[rng[0] : rng[1]]
        except (TypeError, IndexError):
            pass
    return html.unescape(_T_CO.sub("", full)).strip()


def _media(legacy: dict) -> tuple[Media | None, str | None, str]:
    """返回 (可转发媒体, 封面, 时长文本)。视频优先，其次图集"""
    medias = ((legacy.get("extended_entities") or {}).get("media")) or []
    photos: list[str] = []
    for m in medias:
        kind = m.get("type")
        if kind in ("video", "animated_gif"):
            info = m.get("video_info") or {}
            url = _best_mp4(info.get("variants") or [])
            if url:
                secs = int(info.get("duration_millis") or 0) // 1000
                cover = m.get("media_url_https") or None
                return (
                    Media(kind="video", url=url, headers=_MEDIA_HEADERS, duration=secs),
                    cover,
                    format_duration(secs) if secs else "",
                )
        elif kind == "photo" and (u := m.get("media_url_https")):
            photos.append(f"{u}:orig")
    if photos:
        return Media(kind="images", urls=photos, headers=_MEDIA_HEADERS), None, ""
    return None, None, ""


def _best_mp4(variants: list[dict]) -> str | None:
    """挑最高码率的 mp4（hls 无 bitrate 跳过；gif 的 bitrate 为 0 仍取）"""
    best_rate, best_url = -1, None
    for v in variants:
        if v.get("content_type") != "video/mp4":
            continue
        rate = v.get("bitrate")
        if rate is None:
            continue
        if rate >= best_rate and v.get("url"):
            best_rate, best_url = rate, v["url"]
    return best_url


def _kind_label(legacy: dict, media: Media | None) -> str:
    if media is None:
        return ""
    if media.kind == "images":
        return f"（{len(media.urls)}图）"
    medias = ((legacy.get("extended_entities") or {}).get("media")) or []
    if any(m.get("type") == "animated_gif" for m in medias):
        return "（GIF）"
    return "（视频）"


def _stats(node: dict, legacy: dict) -> str:
    parts = []
    count = (node.get("views") or {}).get("count")
    if count and str(count).isdigit():
        parts.append(f"浏览 {format_num(int(count))}")
    parts.append(f"点赞 {format_num(int(legacy.get('favorite_count') or 0))}")
    parts.append(f"评论 {format_num(int(legacy.get('reply_count') or 0))}")
    reposts = int(legacy.get("retweet_count") or 0) + int(
        legacy.get("quote_count") or 0
    )
    parts.append(f"转发 {format_num(reposts)}")
    return " · ".join(parts)


def _repost_line(node: dict, tweet_id: str, tweet_map: dict[str, dict]) -> str:
    """引用/转推/回复的一句话摘要，给主推文补上下文"""
    quoted = node.get("quoted_status_result") or {}
    retweeted = node.get("retweeted_status_result") or {}
    if inner := _node(quoted.get("result") or {}):
        label = "引用"
    elif inner := _node(retweeted.get("result") or {}):
        label = "转发"
    else:
        # 没有原生引用/转推时，若是回复则补上被回复的父推文
        legacy = node.get("legacy") or {}
        parent_id = legacy.get("in_reply_to_status_id_str") or legacy.get(
            "conversation_id_str"
        )
        if not parent_id or parent_id == tweet_id:
            return ""
        inner = tweet_map.get(parent_id)
        if inner is None:
            return ""
        label = "回复"
    author, _ = _author(inner)
    text = _text(inner.get("legacy") or {})
    return f"{label} {author}：{truncate(text, 50)}" if text else f"{label} {author}"
