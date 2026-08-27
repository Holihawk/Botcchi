"""网易云点歌：搜索歌曲并发送可播放的音乐分享卡片。

网易云为国内资源，client 用 trust_env=False 绕开系统代理直连。
"""

from dataclasses import dataclass

import httpx
from loguru import logger

from .tools.registry import Tool, ToolContext, tool

SEARCH_API = "https://music.163.com/api/search/get/web"
SONG_URL = "https://music.163.com/#/song?id={}"
HEADERS = {
    "Referer": "https://music.163.com",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
}


@dataclass(slots=True)
class Song:
    id: int
    name: str
    artists: str


class MusicService:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(
            timeout=15, trust_env=False, headers=HEADERS
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def search(self, keyword: str, limit: int = 5) -> list[Song]:
        resp = await self.client.get(
            SEARCH_API,
            params={"s": keyword, "type": 1, "offset": 0, "limit": limit},
        )
        resp.raise_for_status()
        songs = (resp.json().get("result") or {}).get("songs") or []
        return [
            Song(
                id=int(s["id"]),
                name=str(s.get("name", "")),
                artists="/".join(
                    str(a.get("name", "")) for a in s.get("artists") or []
                ),
            )
            for s in songs
            if s.get("id")
        ]


def build_music_tool(music: MusicService) -> Tool:
    @tool(
        name="share_music",
        description=(
            "网易云点歌：搜索歌曲并往聊天里发一张可直接播放的音乐卡片。"
            "用户点歌/让你推荐歌/聊到某首歌想听时调用。"
            "keyword 填歌名，artist 可选填歌手以精确匹配。"
            "调用成功后简短报一下歌名即可，卡片已经发出去了。"
        ),
    )
    async def share_music(
        ctx: ToolContext, keyword: str, artist: str = ""
    ) -> str:
        query = f"{keyword.strip()} {artist.strip()}".strip()
        if not query:
            return "keyword 不能为空"
        try:
            songs = await music.search(query)
        except Exception as e:
            logger.warning(f"点歌搜索失败 {query}: {e}")
            return "网易云搜索暂时不可用，请如实告知用户"
        if not songs:
            return f"没有搜到「{query}」，可以换个写法或问用户歌手是谁"
        song = songs[0]
        try:
            await ctx.channel.send_music(
                ctx.incoming.session, "163", str(song.id)
            )
        except NotImplementedError:
            return (
                f"当前渠道不支持音乐卡片，可把链接发给用户："
                f"{song.name} - {song.artists} " + SONG_URL.format(song.id)
            )
        except Exception:
            logger.exception(f"音乐卡片发送失败: {song.id}")
            return "音乐卡片没发出去，稍后再试"
        return f"已点歌：{song.name} - {song.artists}（卡片已发送）"

    return share_music


def build_poke_tool() -> Tool:
    @tool(
        name="poke_user",
        description=(
            "QQ 戳一戳：轻戳一下用户（群里戳群成员，私聊戳好友）。"
            "用户求戳、想被注意，或你想俏皮地回应/提醒某人时调用。"
            "user_id 留空则戳当前对话的用户。别连续戳太多次。"
        ),
    )
    async def poke_user(ctx: ToolContext, user_id: str = "") -> str:
        target = user_id.strip() or ctx.incoming.user_id
        try:
            ok = await ctx.channel.poke(ctx.incoming.session, target)
        except NotImplementedError:
            return "当前渠道不支持戳一戳"
        return "戳啦~" if ok else "没戳到（可能不在本群或不是好友）"

    return poke_user
