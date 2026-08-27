"""直播间：房间信息 + 主播信息。

web 端的 getInfoByRoom 对免登录请求一律风控，改用老的 Room/get_info
（房间号、短号都认），主播昵称另取 Master/info。
"""

import httpx
from loguru import logger

from ..base import ParsedLink, format_num, truncate
from . import api

ROOM_API = "https://api.live.bilibili.com/room/v1/Room/get_info"
MASTER_API = "https://api.live.bilibili.com/live_user/v1/Master/info"

_STATUS = {0: "未开播", 1: "直播中", 2: "轮播中"}


async def parse_live(client: httpx.AsyncClient, room_id: str) -> ParsedLink:
    room = await api.call(
        client,
        ROOM_API,
        {"room_id": room_id},
        referer=f"https://live.bilibili.com/{room_id}",
    )
    author, followers = await _anchor(client, room.get("uid"))
    real_id = room.get("room_id") or room_id
    area = " · ".join(
        part
        for part in (room.get("parent_area_name"), room.get("area_name"))
        if part
    )
    stats = " · ".join(
        part
        for part in (
            _STATUS.get(room.get("live_status", 0), "未开播"),
            f"分区 {area}" if area else "",
            f"人气 {format_num(int(room.get('online') or 0))}",
            f"粉丝 {format_num(followers)}" if followers else "",
        )
        if part
    )
    return ParsedLink(
        platform="B站直播",
        title=room.get("title", ""),
        author=author,
        author_label="主播",
        stats=stats,
        extra=truncate(f"标签：{room['tags']}") if room.get("tags") else "",
        url=f"https://live.bilibili.com/{real_id}",
        # 开播时关键帧比封面更能反映当下内容
        cover=(room.get("keyframe") if room.get("live_status") == 1 else None)
        or room.get("user_cover")
        or None,
    )


async def _anchor(client: httpx.AsyncClient, uid: int | None) -> tuple[str, int]:
    """主播昵称与粉丝数；取不到不影响直播间卡片。"""
    if not uid:
        return "", 0
    try:
        data = await api.call(client, MASTER_API, {"uid": uid})
    except Exception:
        logger.warning(f"B站主播信息获取失败: {uid}")
        return "", 0
    return (data.get("info") or {}).get("uname", ""), int(
        data.get("follower_num") or 0
    )
