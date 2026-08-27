"""AcFun 视频解析：ac 号链接，页面内嵌 videoInfo，流为 m3u8（ffmpeg 落盘）。"""

import json
import re

import httpx

from .base import (
    Media,
    ParsedLink,
    ParseError,
    Pattern,
    format_duration,
    format_num,
    truncate,
)

_AC = re.compile(r"acfun\.cn/v/ac(?P<acid>\d+)")
_INFO = re.compile(r"window\.videoInfo\s*=(.*?)</script>", re.DOTALL)

PATTERNS: list[Pattern] = [("acfun.cn", _AC)]

_HEADERS = {"Referer": "https://www.acfun.cn/"}


async def parse(client: httpx.AsyncClient, match: re.Match[str]) -> ParsedLink:
    acid = match.group("acid")
    url = f"https://www.acfun.cn/v/ac{acid}"
    resp = await client.get(
        f"{url}?quickViewId=videoInfo_new&ajaxpipe=1",
        headers=_HEADERS,
        follow_redirects=True,
    )
    resp.raise_for_status()
    matched = _INFO.search(resp.text)
    if matched is None:
        raise ParseError("AcFun 页面无 videoInfo")
    # ajaxpipe 输出里 JSON 被多层转义
    raw = re.sub(r'\\{1,4}"', '"', matched.group(1))
    raw = raw.replace('"{', "{").replace('}"', "}")
    info = json.loads(raw)

    user = info.get("user") or {}
    duration = int(info.get("durationMillis") or 0) // 1000
    m3u8_url = ""
    try:
        ks_raw = info["currentVideoInfo"]["ksPlayJson"]
        # 反转义后 ksPlayJson 可能已被展开成对象
        ks_play = ks_raw if isinstance(ks_raw, dict) else json.loads(ks_raw)
        representations = ks_play["adaptationSet"][0]["representation"]
        # 取最低清晰度，群转发画质够用且体积小
        m3u8_url = representations[-1]["url"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        pass

    media = (
        Media(
            kind="m3u8", url=m3u8_url, headers=_HEADERS, duration=duration
        )
        if m3u8_url
        else None
    )
    return ParsedLink(
        platform="AcFun",
        title=info.get("title", ""),
        author=user.get("name", ""),
        author_label="UP主",
        duration=format_duration(duration),
        stats=(
            f"播放 {format_num(int(info.get('viewCount') or 0))} · "
            f"弹幕 {format_num(int(info.get('danmakuCount') or 0))}"
        ),
        extra=truncate(info.get("description", "")),
        url=url,
        cover=info.get("coverUrl") or None,
        cover_referer="https://www.acfun.cn/",
        media=media,
    )
