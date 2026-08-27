"""网易云音乐解析：163cn.tv 短链、song 链接，发歌曲音频 + 封面。

官方接口需登录/加密，参照 nonebot-plugin-parser-lite 走 toubiec.cn 中转
（AES-GCM 握手）。中转服务失效时解析静默失败，只影响该平台。
"""

import base64
import json
import os
import re
import time

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .base import Media, ParsedLink, ParseError, Pattern, format_duration

API_BASE = "https://nextmusic.toubiec.cn/api"

_SHORT = re.compile(r"163cn\.tv/[a-zA-Z0-9]+")
_SONG = re.compile(r"music\.163\.com/(?:[#/]*)song(?:/|\?id=)(?P<id>\d+)")
_SONG_PARAM = re.compile(r"music\.163\.com/[^\s]*?[?&]id=(?P<id>\d+)")

PATTERNS: list[Pattern] = [
    ("163cn.tv", _SHORT),
    ("music.163.com", _SONG),
    ("music.163.com", _SONG_PARAM),
]

_HEADERS = {"Referer": "https://wyapi.toubiec.cn/"}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _encrypt(payload: dict, key_b64: str) -> str:
    key = base64.b64decode(key_b64)
    iv = os.urandom(12)
    encrypted = AESGCM(key).encrypt(
        iv, json.dumps(payload, separators=(",", ":")).encode(), None
    )
    return f"{_b64(iv)}.{_b64(encrypted[-16:])}.{_b64(encrypted[:-16])}"


def _decrypt(ciphertext: str, key_b64: str) -> dict:
    key = base64.b64decode(key_b64)
    iv_b64, tag_b64, data_b64 = ciphertext.split(".")
    combined = base64.b64decode(data_b64) + base64.b64decode(tag_b64)
    plain = AESGCM(key).decrypt(base64.b64decode(iv_b64), combined, None)
    return json.loads(plain)


def _duration_seconds(duration: str) -> int:
    parts = [int(p) for p in str(duration).split(":") if p.strip().isdigit()]
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


async def _fetch(
    client: httpx.AsyncClient, session: dict, endpoint: str, payload: dict
) -> dict:
    body = {
        "keyId": session["keyId"],
        "keyToken": session["keyToken"],
        "data": _encrypt(
            {**payload, "timestamp": int(time.time() * 1000)}, session["key"]
        ),
    }
    resp = await client.post(
        f"{API_BASE}/{endpoint}", json=body, headers=_HEADERS
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") != 200:
        raise ParseError(f"网易云中转接口错误: {result.get('msg') or result}")
    if ciphertext := result.get("ciphertext"):
        return _decrypt(ciphertext, session["key"])["data"]
    return result["data"]


async def parse(client: httpx.AsyncClient, match: re.Match[str]) -> ParsedLink:
    text = match.group(0)
    if _SHORT.fullmatch(text):
        resp = await client.get(f"https://{text}", follow_redirects=True)
        final = str(resp.url)
        match = _SONG.search(final) or _SONG_PARAM.search(final)  # type: ignore[assignment]
        if match is None:
            raise ParseError(f"163cn.tv 短链未指向歌曲: {final}")
    song_id = match.group("id")

    key_resp = await client.post(f"{API_BASE}/key", headers=_HEADERS)
    key_body = key_resp.json()
    if key_body.get("code") != 200:
        raise ParseError("网易云中转握手失败")
    session = key_body["data"]

    song = await _fetch(client, session, "getSongInfo", {"id": song_id})
    url_data = await _fetch(
        client, session, "getSongUrl", {"id": song_id, "level": "standard"}
    )
    audio_url = url_data.get("url")
    if not audio_url:
        raise ParseError("拿不到音频地址（可能是 VIP 曲目）")
    seconds = _duration_seconds(song.get("duration", "0"))
    return ParsedLink(
        platform="网易云音乐",
        title=song.get("name", "未知曲目"),
        author=song.get("singer", ""),
        author_label="歌手",
        duration=format_duration(seconds),
        url=f"https://music.163.com/song/{song_id}",
        cover=song.get("picimg") or None,
        media=Media(
            kind="audio", url=audio_url, headers=_HEADERS, duration=seconds
        ),
        cache_key=f"ncm:{song_id}",
    )
