"""签到/个人信息卡片：横版磨砂玻璃风，元素沿用旧版 Botcchi 签到卡。

素材与字体来自旧版 resources/themes/default/assets/sign（打包在 assets/）。
静态 SVG 没有 backdrop-filter，毛玻璃 = 背景模糊色块 + 半透明白面板 + 白描边
+ 顶部高光。布局 920x540 逻辑像素。

主题按时段切换：6-18 点粉色全量布局；0-6 与 18-24 点暗色夜间布局（略简化：
省去吊牌/天气/立绘等贴纸，换月亮与星星）。
"""

import random
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

import httpx
from loguru import logger

from ..render import RenderCache, SvgRenderer, esc, image_data_uri
from .service import SignResult, SocialProfile
from .shop import GOODS

W, H = 920, 540
ASSETS = Path(__file__).parent / "assets"
FONT_DIR = ASSETS / "font"
QQ_AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={}&s=160"

# 旧版配色（粉色主题沿用）
PINK = "#D47E8F"
PINK_DARK = "#953B50"
HEAD_TEXT = "#C25C79"
BAR_BORDER = "#DF9DA8"
BANNER_TEXT = "#FBE4E4"

# 旧版字体。注意：resvg 的 fontdb 只索引基础族名（不含「95/700W」等字重后缀），
# 且首选名未命中时不会尝试列表后备名而是直接用默认字体——首选名必须精确命中。
F_CR105 = "寒蝉团圆圆S, sans-serif"  # ChillReunion_105S（与 65S 同族名）
F_CR95 = "寒蝉团圆圆, sans-serif"  # ChillReunion_95
F_CR65S = "寒蝉团圆圆S, sans-serif"  # ChillReunion_65S
F_SH = "Noto Sans CJK SC, sans-serif"  # 思源黑体（系统 Noto 同款）
F_JCYT = "江城圆体, sans-serif"
F_RXK = "日系小熊可爱体, sans-serif"

MORNING_MESSAGES = [
    "早上好，希望今天是美好的一天！",
    "醒了吗，今天也要元气满满哦！",
    "早安，愿你拥有美好的一天！",
]
NIGHT_MESSAGES = [
    "今天要早点休息哦~",
    "可不要熬夜到太晚呀",
    "不要熬夜啦！",
]

Theme = Literal["pink", "dark"]


def resolve_theme(hour: int) -> Theme:
    """0-6 与 18-24 点为暗色，其余为粉色"""
    return "dark" if hour < 6 or hour >= 18 else "pink"


@dataclass(slots=True)
class CardData:
    profile: SocialProfile
    mode: Literal["sign", "profile"]
    """sign = 今日签到（奖励视图）；profile = 我的信息（已签/查询视图）"""
    message: str
    avatar: str | None = None
    """data URI；None 时画首字符占位头像"""
    impression_added: float = 0.0
    coins_added: int = 0
    streak_bonus: int = 0
    double: bool = False
    double_card: bool = False
    theme: Theme | None = None
    """None 时按当前时段自动选择"""


def _tw(text: str, size: float) -> float:
    """估算文本宽度：CJK 全宽、其余按 0.55 字宽"""
    return sum(size if ord(c) > 0x2E7F else size * 0.55 for c in text)


def _fit(text: str, size: float, max_w: float) -> str:
    if _tw(text, size) <= max_w:
        return text
    while text and _tw(text + "…", size) > max_w:
        text = text[:-1]
    return text + "…"


def _wrap(text: str, size: float, max_w: float, max_lines: int = 2) -> list[str]:
    lines: list[str] = []
    cur = ""
    for ch in text:
        if _tw(cur + ch, size) > max_w and cur:
            lines.append(cur)
            cur = ""
        cur += ch
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _fit(lines[-1] + "…", size, max_w)
    return lines


@lru_cache(maxsize=64)
def _asset_uri(rel: str) -> str:
    return image_data_uri((ASSETS / "img" / rel).read_bytes())


def _img(rel: str, x: float, y: float, w: float, h: float) -> str:
    return (
        f'<image x="{x}" y="{y}" width="{w}" height="{h}"'
        f' href="{_asset_uri(rel)}"/>'
    )


def _text(
    x: float, y: float, size: float, family: str, content: str,
    fill: str = PINK, anchor: str = "start", weight: str | None = None,
) -> str:
    w = f' font-weight="{weight}"' if weight else ""
    a = f' text-anchor="{anchor}"' if anchor != "start" else ""
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" font-family="{family}"'
        f'{w}{a} fill="{fill}">{esc(content)}</text>'
    )


def _glass(
    x: float, y: float, w: float, h: float, rx: float,
    fill_opacity: float, stroke_opacity: float, sheen_opacity: float = 1.0,
    shadow: bool = False,
) -> str:
    filt = ' filter="url(#panel-shadow)"' if shadow else ""
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}"'
        f' fill="#ffffff" fill-opacity="{fill_opacity}"'
        f' stroke="#ffffff" stroke-opacity="{stroke_opacity}"'
        f' stroke-width="1.4"{filt}/>'
        f'<rect x="{x + 1.5}" y="{y + 1.5}" width="{w - 3}" height="{h * 0.42}"'
        f' rx="{rx - 1.5}" fill="url(#sheen)" opacity="{sheen_opacity}"/>'
    )


def _sparkle(cx: float, cy: float, r: float, opacity: float,
             fill: str = "#ffffff") -> str:
    q = r * 0.16
    return (
        f'<path d="M {cx} {cy - r} Q {cx + q} {cy - q} {cx + r} {cy}'
        f' Q {cx + q} {cy + q} {cx} {cy + r}'
        f' Q {cx - q} {cy + q} {cx - r} {cy}'
        f' Q {cx - q} {cy - q} {cx} {cy - r} Z"'
        f' fill="{fill}" fill-opacity="{opacity}"/>'
    )


def _defs(bg_stops: str) -> str:
    return f"""<defs>
  <linearGradient id="bg-grad" x1="0" y1="0" x2="0" y2="1">{bg_stops}</linearGradient>
  <linearGradient id="sheen" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#ffffff" stop-opacity="0.45"/>
    <stop offset="1" stop-color="#ffffff" stop-opacity="0"/>
  </linearGradient>
  <filter id="blob-blur" x="-60%" y="-60%" width="220%" height="220%">
    <feGaussianBlur stdDeviation="42"/>
  </filter>
  <filter id="panel-shadow" x="-20%" y="-20%" width="140%" height="140%">
    <feDropShadow dx="0" dy="8" stdDeviation="12"
      flood-color="#000000" flood-opacity="0.22"/>
  </filter>
  <filter id="ava-shadow" x="-30%" y="-30%" width="160%" height="160%">
    <feDropShadow dx="0" dy="0" stdDeviation="5"
      flood-color="#D6A7A7" flood-opacity="0.9"/>
  </filter>
  <clipPath id="ava-clip"><circle cx="100" cy="105" r="52"/></clipPath>
</defs>"""


def _avatar(data: CardData, ring: str, text_fill: str) -> str:
    if data.avatar:
        body = (
            f'<circle cx="100" cy="105" r="52" fill="#ffffff"'
            f' filter="url(#ava-shadow)"/>'
            f'<image x="48" y="53" width="104" height="104" href="{data.avatar}"'
            f' clip-path="url(#ava-clip)" preserveAspectRatio="xMidYMid slice"/>'
            f'<circle cx="100" cy="105" r="53" fill="none" stroke="{ring}"'
            f' stroke-width="3"/>'
        )
    else:
        initial = (data.profile.user_name or "?")[0]
        body = (
            f'<circle cx="100" cy="105" r="52" fill="{PINK}"'
            f' filter="url(#ava-shadow)"/>'
            + _text(100, 124, 52, F_CR105, initial, text_fill, anchor="middle")
        )
    return body + _pendant_overlay(data.profile.pendant)


def _pendant_overlay(pendant_id: str | None) -> str:
    """佩戴中的头像挂件贴图；锚点参数见 shop.PendantSpec（头像框 48,53 起）"""
    goods = GOODS.get(pendant_id) if pendant_id else None
    if goods is None or goods.pendant is None:
        return ""
    spec = goods.pendant
    x, y, w = 48 + spec.dx, 53 + spec.dy, spec.width
    rotate = (
        f' transform="rotate({spec.rotate} {x + w / 2} {y + w / 2})"'
        if spec.rotate
        else ""
    )
    return (
        f'<image x="{x}" y="{y}" width="{w}" height="{w}"'
        f' href="{_asset_uri(spec.asset)}"{rotate}/>'
    )


def _content_lines(data: CardData) -> tuple[str, list[str]]:
    p = data.profile
    if data.mode == "sign":
        double = ""
        if data.double:
            double = " (双倍卡!)" if data.double_card else " (双倍!)"
        return "今日签到", [
            f"好感度 +{data.impression_added:.2f}{double}",
            f"金币 +{data.coins_added}",
            f"连续签到{p.streak}天"
            + (f" (加成+{data.streak_bonus})" if data.streak_bonus else ""),
        ]
    return "我的信息", [
        f"金币: {p.coins}",
        f"排名: {p.rank if p.rank else '未上榜'}",
        f"上次签到：{p.last_sign_date or '从未'}",
    ]


# ---- 粉色主题（6-18 点，全量元素） ------------------------------------------


def _pink_background() -> str:
    blobs = [
        (130, 80, 100, "#ff9ec3", 0.55),
        (820, 120, 90, "#d9b8ff", 0.45),
        (480, 40, 70, "#ffd2a8", 0.5),
        (200, 500, 110, "#ffc9de", 0.6),
        (760, 470, 100, "#ff8fb4", 0.5),
    ]
    parts = [f'<rect width="{W}" height="{H}" fill="url(#bg-grad)"/>']
    parts += [
        f'<circle cx="{x}" cy="{y}" r="{r}" fill="{c}" fill-opacity="{o}"'
        f' filter="url(#blob-blur)"/>'
        for x, y, r, c, o in blobs
    ]
    parts += [
        _sparkle(48, 260, 9, 0.9),
        _sparkle(890, 300, 11, 0.85),
        _sparkle(470, 522, 8, 0.85),
        _sparkle(560, 24, 7, 0.9),
    ]
    return "".join(parts)


def _pink_banner(data: CardData) -> str:
    """旧版签到横幅：错位纸层 + 日历图标 + 累计签到 + 问候语 + 章鱼贴纸"""
    parts = [
        f'<rect x="566" y="58" width="300" height="84" rx="12"'
        f' fill="{PINK_DARK}"/>',
        f'<rect x="560" y="50" width="300" height="84" rx="12" fill="{PINK}"/>',
        _img("rl.png", 574, 64, 56, 56),
        _text(644, 94, 24, F_SH, f"累计签到{data.profile.sign_count}天",
              BANNER_TEXT, weight="700"),
    ]
    for i, line in enumerate(_wrap(data.message, 13, 205)):
        parts.append(_text(644, 114 + i * 17, 13, F_CR65S, line, BANNER_TEXT))
    parts.append(_img("2.png", 828, 106, 66, 45))
    return "".join(parts)


def _build_pink(data: CardData) -> str:
    p = data.profile
    lv = p.level
    title, lines = _content_lines(data)
    progress = lv.progress(p.impression)
    name = _fit(p.user_name or p.user_id, 30, 300)

    left_panel = [
        _glass(42, 168, 400, 282, 20, 0.35, 0.7),
        _img(f"tag/{random.randint(0, 5)}.png", 62, 190, 62, 95),
        _text(144, 230, 36, F_JCYT, title, PINK),
    ]
    left_panel += [
        _text(242, y, 22, F_JCYT, line, PINK, anchor="middle")
        for y, line in zip((292, 340, 388), lines)
    ]

    hearts = "".join(
        _img("h2.png" if i < lv.level else "h1.png", 482 + i * 31, 232, 26, 26)
        for i in range(8)
    )
    right_panel = [
        _glass(462, 168, 436, 282, 20, 0.35, 0.7),
        _text(482, 216, 26, F_RXK, f"当前好感度: {p.impression:.2f}", PINK),
        hearts,
        _text(482, 302, 19, F_RXK, f"好感度等级: {lv.level}", PINK),
        _text(482, 332, 19, F_RXK, lv.mood, PINK),
        _text(482, 362, 19, F_RXK,
              f"距离升级还差{lv.to_next(p.impression):.2f}好感度", PINK),
        f'<rect x="482" y="384" width="296" height="24" fill="#ffffff"'
        f' fill-opacity="0.45" stroke="{BAR_BORDER}" stroke-width="1"/>',
        _img(f"weather/{random.randint(0, 11)}.png", 834, 186, 48, 48),
        _text(858, 260, 20, F_RXK, f"{random.randint(1, 40)}℃", PINK,
              anchor="middle"),
    ]
    if progress > 0:
        right_panel.insert(
            7,
            f'<rect x="483" y="385" width="{max(8.0, 294 * progress):.1f}"'
            f' height="22" fill="{PINK}"/>',
        )

    return (
        _pink_background()
        + _glass(22, 22, 876, 496, 26, 0.42, 0.75, shadow=True)
        + _avatar(data, "#ffffff", BANNER_TEXT)
        + _text(170, 95, 30, F_CR105, name, HEAD_TEXT)
        + _text(170, 125, 16, F_CR95, f"UID:{p.user_id}", HEAD_TEXT)
        + _pink_banner(data)
        + "".join(left_panel)
        + "".join(right_panel)
        + _text(460, 492, 14, F_RXK, f"{datetime.now():%Y-%m-%d %H:%M:%S}",
                "#C78BA5", anchor="middle")
        + _img(f"mbl/{random.randint(0, 3)}.png", 792, 388, 100, 126)
    )


# ---- 暗色主题（0-6 / 18-24 点，简化布局） -----------------------------------

D_TEXT = "#F2C9D9"
D_SUB = "#B78BA0"
D_ACCENT = "#FF9DC0"


def _dark_background() -> str:
    blobs = [
        (150, 100, 110, "#7B3A5C", 0.5),
        (780, 420, 120, "#4A2A5E", 0.45),
        (520, 540, 90, "#8A4668", 0.35),
    ]
    stars = [
        (200, 60, 1.6), (320, 110, 1.2), (620, 45, 1.8), (720, 150, 1.2),
        (80, 330, 1.4), (880, 240, 1.6), (420, 500, 1.3), (540, 90, 1.1),
    ]
    parts = [f'<rect width="{W}" height="{H}" fill="url(#bg-grad)"/>']
    parts += [
        f'<circle cx="{x}" cy="{y}" r="{r}" fill="{c}" fill-opacity="{o}"'
        f' filter="url(#blob-blur)"/>'
        for x, y, r, c, o in blobs
    ]
    parts += [
        f'<circle cx="{x}" cy="{y}" r="{r}" fill="#ffffff"'
        f' fill-opacity="0.75"/>'
        for x, y, r in stars
    ]
    parts += [
        _sparkle(852, 300, 10, 0.7, "#F7E3B0"),
        _sparkle(100, 470, 8, 0.6),
        # 弯月
        '<path d="M 846 46 A 30 30 0 1 1 838 104 A 36 36 0 0 0 846 46 Z"'
        ' fill="#F2D9A0" fill-opacity="0.9"/>',
    ]
    return "".join(parts)


def _build_dark(data: CardData) -> str:
    p = data.profile
    lv = p.level
    title, lines = _content_lines(data)
    progress = lv.progress(p.impression)
    name = _fit(p.user_name or p.user_id, 30, 300)

    hearts = "".join(
        _img("h2.png" if i < lv.level else "h1.png", 500 + i * 31, 240, 26, 26)
        for i in range(8)
    )
    panel = [
        _glass(42, 168, 856, 282, 20, 0.07, 0.18, sheen_opacity=0.25),
        _text(70, 232, 34, F_JCYT, title, D_ACCENT),
        *(
            _text(70, y, 22, F_JCYT, line, D_TEXT)
            for y, line in zip((288, 330, 372), lines)
        ),
        _text(500, 222, 24, F_RXK, f"当前好感度: {p.impression:.2f}", D_TEXT),
        hearts,
        _text(500, 322, 18, F_RXK,
              f"好感度等级: {lv.level} · {lv.mood}", D_SUB),
        _text(500, 352, 18, F_RXK,
              f"距离升级还差{lv.to_next(p.impression):.2f}好感度", D_SUB),
        '<rect x="500" y="376" width="330" height="20" fill="#ffffff"'
        ' fill-opacity="0.08" stroke="#8A5B74" stroke-width="1"/>',
    ]
    if progress > 0:
        panel.append(
            f'<rect x="501" y="377" width="{max(8.0, 328 * progress):.1f}"'
            f' height="18" fill="{PINK}"/>'
        )

    chip = (
        f'<rect x="560" y="58" width="280" height="64" rx="12"'
        f' fill="{D_ACCENT}" fill-opacity="0.14"'
        f' stroke="{D_ACCENT}" stroke-opacity="0.35" stroke-width="1.2"/>'
        + _img("rl.png", 572, 68, 44, 44)
        + _text(628, 100, 22, F_SH, f"累计签到{p.sign_count}天",
                D_ACCENT, weight="700")
    )

    return (
        _dark_background()
        + _glass(22, 22, 876, 496, 26, 0.06, 0.16, sheen_opacity=0.25,
                 shadow=True)
        + _avatar(data, "#ffffff55", "#3A2230")
        + _text(170, 95, 30, F_CR105, name, D_TEXT)
        + _text(170, 125, 16, F_CR95, f"UID:{p.user_id}", D_SUB)
        + chip
        + "".join(panel)
        + _text(70, 492, 14, F_CR65S, data.message, D_SUB)
        + _text(870, 492, 14, F_RXK, f"{datetime.now():%Y-%m-%d %H:%M:%S}",
                D_SUB, anchor="end")
    )


def build_card(data: CardData) -> str:
    theme = data.theme or resolve_theme(datetime.now().hour)
    if theme == "dark":
        stops = (
            '<stop offset="0" stop-color="#241722"/>'
            '<stop offset="1" stop-color="#120B12"/>'
        )
        body = _build_dark(data)
    else:
        stops = (
            '<stop offset="0" stop-color="#FFD9E8"/>'
            '<stop offset="0.55" stop-color="#FBE4E4"/>'
            '<stop offset="1" stop-color="#FFF3F7"/>'
        )
        body = _build_pink(data)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}"'
        f' viewBox="0 0 {W} {H}" font-family="sans-serif">'
        + _defs(stops)
        + body
        + "</svg>"
    )


class SocialCards:
    """卡片出图：头像抓取 + 问候语 + SVG 渲染（附带旧版卡片字体）"""

    def __init__(self, renderer: SvgRenderer, cache: RenderCache | None = None):
        settings = renderer.settings.model_copy(deep=True)
        settings.font_dirs = [*settings.font_dirs, str(FONT_DIR)]
        self.renderer = SvgRenderer(settings)
        self.cache = cache
        """商店等内容稳定卡片的出图缓存；签到卡带头像/时间不走缓存"""

    async def sign_card(self, result: SignResult, bot_name: str) -> bytes:
        # 与旧版一致：当天已签过则展示「我的信息」视图
        data = CardData(
            profile=result.profile,
            mode="sign" if result.signed else "profile",
            message=self._greeting(bot_name),
            avatar=await self._fetch_avatar(result.profile.user_id),
            impression_added=result.impression_added,
            coins_added=result.coins_added,
            streak_bonus=result.streak_bonus,
            double=result.double,
            double_card=result.double_card,
        )
        return await self.renderer.render(build_card(data))

    async def shop_card(self, entries) -> bytes:
        from .shop_card import build_shop_card

        svg = build_shop_card(entries)
        if self.cache is not None:
            return await self.cache.render(self.renderer, svg)
        return await self.renderer.render(svg)

    async def profile_card(self, profile: SocialProfile, bot_name: str) -> bytes:
        data = CardData(
            profile=profile,
            mode="profile",
            message=self._greeting(bot_name),
            avatar=await self._fetch_avatar(profile.user_id),
        )
        return await self.renderer.render(build_card(data))

    async def _fetch_avatar(self, user_id: str) -> str | None:
        if not user_id.isdigit():
            return None
        try:
            # trust_env=False：QQ 头像为国内直连资源，绕开 http(s)_proxy 环境变量
            async with httpx.AsyncClient(
                timeout=8, trust_env=False, follow_redirects=True
            ) as client:
                resp = await client.get(QQ_AVATAR_URL.format(user_id))
                resp.raise_for_status()
            mime = resp.headers.get("content-type", "image/jpeg").split(";")[0]
            return image_data_uri(resp.content, mime)
        except Exception as e:
            logger.warning(f"头像获取失败 {user_id}: {e}")
            return None

    def _greeting(self, bot_name: str) -> str:
        hour = datetime.now().hour
        if 6 <= hour < 10:
            return random.choice(MORNING_MESSAGES)
        if hour < 6 or hour >= 22:
            return random.choice(NIGHT_MESSAGES)
        return f"{bot_name}希望你开心！"
