"""B站接口调用：设备指纹 cookie、WBI 签名与统一的返回码校验。

动态/图文类接口对没有设备指纹的请求判风控（code=-352），buvid3/buvid4 由
finger/spi 免登录下发，进程内缓存复用；WBI 签名同样不需要登录态。
"""

import time
from functools import reduce
from hashlib import md5
from urllib.parse import urlencode

import httpx
from loguru import logger

from ..base import ParseError

SPI_API = "https://api.bilibili.com/x/frontend/finger/spi"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"

WEB_HEADERS = {
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}

# fmt: off
_MIXIN_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)
"""WBI 密钥的字符重排表，取自 B站 web 端"""
# fmt: on

_CODE_HINTS = {
    -352: "被B站风控拦截，过一会儿再试",
    -403: "该内容需要登录或无权访问",
    -404: "内容不存在或已被删除",
    -509: "请求过于频繁，过一会儿再试",
    62002: "视频已被 UP 主设为私密",
    62004: "视频还在审核中",
}

_buvid: dict[str, str] = {}
_wbi_keys: tuple[str, str] | None = None


async def buvid_cookies(client: httpx.AsyncClient) -> dict[str, str]:
    """取一次设备指纹；拿不到就裸请求（大概率被风控，但不该拖垮解析）"""
    global _buvid
    if not _buvid:
        try:
            resp = await client.get(SPI_API, headers=WEB_HEADERS)
            data = resp.json()["data"]
            _buvid = {"buvid3": data["b_3"], "buvid4": data["b_4"]}
        except Exception:
            logger.warning("B站设备指纹获取失败，接口可能被风控")
            return {}
    return _buvid


async def _wbi_key_pair(client: httpx.AsyncClient) -> tuple[str, str] | None:
    """nav 接口未登录也会下发 wbi_img，从图片文件名取出两段密钥"""
    global _wbi_keys
    if _wbi_keys is None:
        try:
            resp = await client.get(NAV_API, headers=WEB_HEADERS)
            wbi = resp.json()["data"]["wbi_img"]
            _wbi_keys = (
                wbi["img_url"].rsplit("/", 1)[1].split(".")[0],
                wbi["sub_url"].rsplit("/", 1)[1].split(".")[0],
            )
        except Exception:
            logger.warning("B站 WBI 密钥获取失败，改为不签名请求")
            return None
    return _wbi_keys


def _sign(params: dict, keys: tuple[str, str]) -> dict:
    mixin = reduce(lambda s, i: s + (keys[0] + keys[1])[i], _MIXIN_TAB, "")[:32]
    signed = {
        k: "".join(c for c in str(v) if c not in "!'()*")
        for k, v in sorted({**params, "wts": int(time.time())}.items())
    }
    signed["w_rid"] = md5((urlencode(signed) + mixin).encode()).hexdigest()
    return signed


async def call(
    client: httpx.AsyncClient,
    url: str,
    params: dict | None = None,
    *,
    buvid: bool = False,
    wbi: bool = False,
    referer: str | None = None,
) -> dict:
    """调 B站 API 取 data 段，非 0 返回码转成带人话提示的 ParseError"""
    global _buvid
    params = dict(params or {})
    if wbi and (keys := await _wbi_key_pair(client)):
        params = _sign(params, keys)
    resp: httpx.Response | None = None
    for attempt in range(2):
        headers = WEB_HEADERS | ({"Referer": referer} if referer else {})
        # 直接拼 Cookie 头：httpx 的按请求 cookies 参数已废弃，也不该污染共享 client
        if buvid and (cookies := await buvid_cookies(client)):
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        resp = await client.get(url, params=params, headers=headers)
        if resp.status_code != 412 or not buvid or attempt:
            break
        # 缓存的设备指纹可能已经被风控，刷新后只重试一次。
        _buvid = {}
        logger.info("B站接口返回 412，刷新设备指纹后重试")
    assert resp is not None
    if resp.status_code == 412:
        raise ParseError("被B站风控拦截，过一会儿再试")
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise ParseError(f"B站接口 HTTP {resp.status_code}") from e
    body = resp.json()
    code = body.get("code")
    if code != 0:
        hint = _CODE_HINTS.get(code) or body.get("message") or "未知错误"
        raise ParseError(f"B站接口 code={code}: {hint}")
    data = body.get("data")
    if not isinstance(data, dict):
        raise ParseError(f"B站接口返回结构异常: {str(body)[:100]}")
    return data
