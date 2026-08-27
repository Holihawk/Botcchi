"""微博访客态：genvisitor2 换取 SUB/SUBP + XSRF-TOKEN。

现在 m.weibo.cn / weibo.com 的内容接口都对无访客态请求 302 到 passport 登录墙，
必须先做一次访客握手。cookie 走请求头显式携带（不污染共享 client 的 cookie jar，
与 bilibili 的 buvid 处理一致），进程内缓存并带过期时间。
"""

import asyncio
import json
import re
import time

import httpx
from loguru import logger

from ..base import ParseError

GENVISITOR = "https://visitor.passport.weibo.cn/visitor/genvisitor2"
_CALLBACK = re.compile(r"visitor_gray_callback\((.*)\)")
_TTL = 3 * 60 * 60  # 访客态有效期，保守取 3 小时

_state: dict[str, object] = {}
_lock = asyncio.Lock()


async def headers(client: httpx.AsyncClient) -> dict[str, str]:
    """返回带访客 cookie 与 XSRF 头的请求头；握手失败抛 ParseError。"""
    cookie, xsrf = await _ensure(client)
    return {
        "Referer": "https://weibo.com/",
        "X-Requested-With": "XMLHttpRequest",
        "X-Xsrf-Token": xsrf,
        "Cookie": f"{cookie}; XSRF-TOKEN={xsrf}",
    }


async def _ensure(client: httpx.AsyncClient) -> tuple[str, str]:
    now = time.monotonic()
    if _state and _state["exp"] > now:  # type: ignore[operator]
        return _state["cookie"], _state["xsrf"]  # type: ignore[return-value]
    async with _lock:
        if _state and _state["exp"] > now:  # type: ignore[operator]
            return _state["cookie"], _state["xsrf"]  # type: ignore[return-value]
        cookie, xsrf = await _handshake(client)
        _state.update(cookie=cookie, xsrf=xsrf, exp=now + _TTL)
        return cookie, xsrf


async def _handshake(client: httpx.AsyncClient) -> tuple[str, str]:
    try:
        resp = await client.post(
            GENVISITOR,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": (
                    "https://visitor.passport.weibo.cn/visitor/visitor?entry=sinawap"
                ),
            },
            data={"cb": "visitor_gray_callback", "tid": "", "new_tid": "null"},
        )
        match = _CALLBACK.search(resp.text)
        if match is None:
            raise ParseError("微博访客握手返回异常")
        data = json.loads(match.group(1)).get("data") or {}
        sub, subp = data.get("sub"), data.get("subp")
        if not sub or not subp:
            raise ParseError("微博访客态获取失败（可能被风控）")
        cookie = f"SUB={sub}; SUBP={subp}"
        # 访问一次 weibo.com 让服务端下发 XSRF-TOKEN（从 Set-Cookie 取，不入 jar）
        landing = await client.get(
            "https://www.weibo.com",
            headers={"Cookie": cookie},
            follow_redirects=True,
        )
        xsrf = ""
        for hop in (*landing.history, landing):
            if "XSRF-TOKEN" in hop.cookies:
                xsrf = hop.cookies["XSRF-TOKEN"]
        return cookie, xsrf
    except ParseError:
        raise
    except Exception as e:
        logger.warning(f"微博访客握手失败: {e}")
        raise ParseError("微博访客态握手失败") from e
