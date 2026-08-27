"""日报数据源：节日倒数/农历、一言、B站热搜、60s、IT资讯、今日新番。

均为国内可直连资源，统一 trust_env=False 绕开本地代理；每个源独立容错，
失败的板块显示占位文案。
"""

import asyncio
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx
from loguru import logger
from lunardate import LunarDate

from ..render import image_data_uri

HITOKOTO_URL = "https://v1.hitokoto.cn/?c=a"
SIX_URL = "https://60s.viki.moe/v2/60s"
BILI_URL = "https://s.search.bilibili.com/main/hotword"
IT_URL = "https://www.ithome.com/rss/"
ANIME_URL = "https://api.bgm.tv/calendar"

NEWS_LIMIT = 11
ANIME_LIMIT = 8
WEEKS = "一二三四五六日"

_LUNAR_MONTHS = "正二三四五六七八九十冬腊"
_LUNAR_DAYS = (
    "初一 初二 初三 初四 初五 初六 初七 初八 初九 初十 "
    "十一 十二 十三 十四 十五 十六 十七 十八 十九 二十 "
    "廿一 廿二 廿三 廿四 廿五 廿六 廿七 廿八 廿九 三十"
).split()


@dataclass(slots=True)
class ReportData:
    day: date
    week: str
    zh_date: str
    festivals: list[tuple[int, str]] = field(default_factory=list)
    hitokoto: str = ""
    bili: list[str] = field(default_factory=list)
    six: list[str] = field(default_factory=list)
    it: list[str] = field(default_factory=list)
    anime: list[tuple[str, str | None]] = field(default_factory=list)
    """(番名, 封面 data URI；抓取失败为 None)"""


def lunar_display(day: date) -> str:
    """公历 → 「六月初五」样式的农历显示"""
    lunar = LunarDate.from_solar_date(day.year, day.month, day.day)
    month = _LUNAR_MONTHS[lunar.month - 1] + "月"
    return ("闰" if lunar.isLeapMonth else "") + month + _LUNAR_DAYS[lunar.day - 1]


def _tomb_sweeping_day(year: int) -> date:
    """清明（寿星公式，21 世纪适用）"""
    y = year % 100
    day = int(y * 0.2422 + 4.81) - y // 4
    return date(year, 4, day)


_LUNAR_FESTIVALS = {"春节": (1, 1), "端午节": (5, 5), "中秋节": (8, 15)}
_FIXED_FESTIVALS = {"元旦": (1, 1), "劳动节": (5, 1), "国庆节": (10, 1)}


def festival_countdowns(today: date) -> list[tuple[int, str]]:
    """(剩余天数, 节日名)，按临近排序；已过的节日算下一年"""
    items: list[tuple[int, str]] = []
    for name, (month, day) in _LUNAR_FESTIVALS.items():
        d = LunarDate(today.year, month, day).to_solar_date()
        if d < today:
            d = LunarDate(today.year + 1, month, day).to_solar_date()
        items.append(((d - today).days, name))
    for name, (month, day) in _FIXED_FESTIVALS.items():
        d = date(today.year, month, day)
        if d < today:
            d = date(today.year + 1, month, day)
        items.append(((d - today).days, name))
    qingming = _tomb_sweeping_day(today.year)
    if qingming < today:
        qingming = _tomb_sweeping_day(today.year + 1)
    items.append(((qingming - today).days, "清明节"))
    items.sort()
    return items


async def _safe(name: str, coro, fallback):
    try:
        return await coro
    except Exception as e:
        logger.warning(f"日报数据源 {name} 获取失败: {e}")
        return fallback


async def fetch_report_data(now: datetime | None = None) -> ReportData:
    now = now or datetime.now()
    # B站等接口拒绝无 UA 请求
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/126.0 Safari/537.36 Botcchi/0.1"
        )
    }
    async with (
        # 国内源直连（绕开本地代理）；bangumi 为境外资源，跟随代理环境变量
        httpx.AsyncClient(
            timeout=15, trust_env=False, follow_redirects=True, headers=headers
        ) as client,
        httpx.AsyncClient(
            timeout=15, follow_redirects=True, headers=headers
        ) as abroad,
    ):
        hitokoto, bili, six, it, anime = await asyncio.gather(
            _safe("一言", _get_hitokoto(client), "今天也要加油哦！"),
            _safe("B站热搜", _get_bili(client), []),
            _safe("60s", _get_six(client), []),
            _safe("IT资讯", _get_it(client), []),
            _safe("新番", _get_anime(abroad, now.weekday()), []),
        )
    return ReportData(
        day=now.date(),
        week=WEEKS[now.weekday()],
        zh_date=lunar_display(now.date()),
        festivals=festival_countdowns(now.date()),
        hitokoto=hitokoto,
        bili=bili,
        six=six,
        it=it,
        anime=anime,
    )


async def _get_hitokoto(client: httpx.AsyncClient) -> str:
    resp = await client.get(HITOKOTO_URL)
    resp.raise_for_status()
    return resp.json()["hitokoto"]


async def _get_bili(client: httpx.AsyncClient) -> list[str]:
    resp = await client.get(BILI_URL)
    resp.raise_for_status()
    return [item["keyword"] for item in resp.json()["list"]][:NEWS_LIMIT - 1]


async def _get_six(client: httpx.AsyncClient) -> list[str]:
    resp = await client.get(SIX_URL)
    resp.raise_for_status()
    return resp.json()["data"]["news"][:NEWS_LIMIT]


async def _get_it(client: httpx.AsyncClient) -> list[str]:
    resp = await client.get(IT_URL)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    titles = [
        item.findtext("title") or "" for item in root.findall("./channel/item")
    ]
    return [t for t in titles if t][:NEWS_LIMIT]


async def _get_anime(
    client: httpx.AsyncClient, weekday: int
) -> list[tuple[str, str | None]]:
    resp = await client.get(ANIME_URL)
    resp.raise_for_status()
    calendar = resp.json()
    day = calendar[weekday] if weekday < len(calendar) else calendar[-1]
    entries = []
    for item in (day.get("items") or [])[:ANIME_LIMIT]:
        name = item.get("name_cn") or item.get("name") or "?"
        images = item.get("images") or {}
        # 封面用中等尺寸即可（展示宽 105px），降低抓取与渲染压力
        url = images.get("common") or images.get("medium") or images.get("large")
        entries.append((name, url))

    async def cover(url: str | None) -> str | None:
        if not url:
            return None
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            mime = resp.headers.get("content-type", "image/jpeg").split(";")[0]
            return image_data_uri(resp.content, mime)
        except Exception:
            return None

    covers = await asyncio.gather(*(cover(url) for _, url in entries))
    return [(name, uri) for (name, _), uri in zip(entries, covers)]
