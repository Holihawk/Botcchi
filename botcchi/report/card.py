"""孤独日报卡片。

578 逻辑像素宽、高度随内容伸缩；粉底白卡、描边分区 + 胶囊标题的原版风格。
素材（头图/图标/方糖体标题字）在 assets/，正文用系统 Noto Sans CJK。
"""

from functools import lru_cache
from pathlib import Path

from ..render import esc, fit_text, image_data_uri, text_width, wrap_text
from .data import ReportData

W = 578
ASSETS = Path(__file__).parent / "assets"
FONT_DIR = ASSETS / "font"

BG = "#ffc0cb"
CARD = "#fffbfc"
BORDER = "#e5aabe"
PILL_TEXT = "#fd9fb5"
TITLE_PINK = "#f7a0b5"
TIP_PINK = "#f7c1d4"
DATE_PINK = "#f384a7"
DATE_BAR = "#f8a1b8"
NAME_PINK = "#dda6b8"
HITO_PINK = "#efacc3"
HOT_RED = "#e38c84"

F_TITLE = "SSFangTangTi, sans-serif"
F_SANS = "Noto Sans CJK SC, sans-serif"


@lru_cache(maxsize=16)
def _asset_uri(rel: str) -> str:
    return image_data_uri((ASSETS / rel).read_bytes())


def _text(
    x: float, y: float, size: float, content: str,
    fill: str = "#4c4c4c", family: str = F_SANS,
    weight: str | None = None, anchor: str = "start",
) -> str:
    w = f' font-weight="{weight}"' if weight else ""
    a = f' text-anchor="{anchor}"' if anchor != "start" else ""
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" font-family="{family}"'
        f'{w}{a} fill="{fill}">{esc(content)}</text>'
    )


def _frame(x: float, y: float, w: float, h: float, title: str, icon: str) -> str:
    """描边分区 + 左上角胶囊标题（含图标）"""
    pill_w = text_width(title, 14) + 45
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10"'
        f' fill="{CARD}" stroke="{BORDER}" stroke-width="3"/>'
        f'<rect x="{x + 12}" y="{y - 14}" width="{pill_w}" height="29" rx="10"'
        f' fill="#fefbfc" stroke="{BORDER}" stroke-width="2"/>'
        f'<image x="{x + 24}" y="{y - 7}" width="15" height="15"'
        f' href="{_asset_uri(f"icon/{icon}")}"/>'
        + _text(x + 44, y + 6, 14, title, PILL_TEXT, weight="700")
    )


def _header(data: ReportData) -> str:
    img_h = 150
    img_w = round(img_h * 1024 / 1365)
    date_x, date_y, date_w = 438, 40, 115
    return (
        f'<image x="25" y="22" width="{img_w}" height="{img_h}"'
        f' href="{_asset_uri("image/1.png")}"/>'
        + _text(150, 105, 58, "孤独日报", TITLE_PINK, family=F_TITLE)
        + _text(160, 145, 25, "BOCCHI NEWS", TIP_PINK, weight="700")
        + f'<rect x="{date_x}" y="{date_y}" width="{date_w}" height="102" rx="10"'
        f' fill="#fdf1f5"/>'
        + _text(
            date_x + date_w / 2, date_y + 42, 34, data.week,
            DATE_PINK, weight="700", anchor="middle",
        )
        + f'<rect x="{date_x}" y="{date_y + 54}" width="{date_w}" height="24"'
        f' fill="{DATE_BAR}"/>'
        + _text(
            date_x + date_w / 2, date_y + 71, 12, data.day.isoformat(),
            "#ffffff", weight="700", anchor="middle",
        )
        + _text(
            date_x + date_w / 2, date_y + 96, 15, data.zh_date,
            DATE_PINK, weight="700", anchor="middle",
        )
    )


def _festivals(x: float, y: float, w: float, h: float, data: ReportData) -> str:
    parts = [_frame(x, y, w, h, "摸鱼日历", "fish.png")]
    line_y = y + 42
    for days, name in data.festivals[:7]:
        parts.append(
            f'<text x="{x + 14}" y="{line_y}" font-size="13" font-family="{F_SANS}"'
            f' fill="#4c4c4c">距离<tspan fill="{NAME_PINK}">{esc(name)}</tspan>'
            f'还剩<tspan fill="{DATE_PINK}" font-weight="700"> {days} </tspan>天</text>'
        )
        line_y += 31
    return "".join(parts)


def _bili(x: float, y: float, w: float, h: float, data: ReportData) -> str:
    parts = [_frame(x, y, w, h, "B站热点", "bilibili.png")]
    items = data.bili or ["获取失败了…"]
    line_y = y + 40
    for s in items:
        parts += [
            f'<rect x="{x + 14}" y="{line_y - 12}" width="15" height="15" rx="2"'
            f' fill="{HOT_RED}"/>',
            _text(x + 21.5, line_y, 10.5, "热", "#ffffff", anchor="middle"),
            _text(x + 36, line_y, 13, fit_text(s, 13, w - 52)),
        ]
        line_y += 24.5
    return "".join(parts)


def _anime(x: float, y: float, w: float, data: ReportData) -> tuple[str, float]:
    cols, tile_w, tile_h = 4, 122, 178
    cover_w, cover_h = 105, 130
    items = data.anime or [("获取失败了…", None)]
    rows = (len(items) + cols - 1) // cols
    h = rows * tile_h + 40
    parts = [_frame(x, y, w, h, "今日新番", "bgm.png")]
    for i, (name, cover) in enumerate(items):
        tx = x + 16 + (i % cols) * (tile_w + 8)
        ty = y + 26 + (i // cols) * tile_h
        cx = tx + (tile_w - cover_w) / 2
        if cover:
            parts.append(
                f'<image x="{cx}" y="{ty}" width="{cover_w}" height="{cover_h}"'
                f' href="{cover}" preserveAspectRatio="xMidYMid slice"/>'
            )
        else:
            parts += [
                f'<rect x="{cx}" y="{ty}" width="{cover_w}" height="{cover_h}"'
                f' fill="#fdf1f5"/>',
                _text(
                    cx + cover_w / 2, ty + cover_h / 2 + 5, 14, "暂无封面",
                    NAME_PINK, anchor="middle",
                ),
            ]
        for j, line in enumerate(wrap_text(name, 12, tile_w - 6, max_lines=2)):
            parts.append(
                _text(
                    tx + tile_w / 2, ty + cover_h + 16 + j * 15, 12, line,
                    "#4c4c4c", anchor="middle",
                )
            )
    return "".join(parts), h


def _news_list(
    x: float, y: float, w: float, title: str, icon: str, items: list[str]
) -> tuple[str, float]:
    items = items or ["获取失败了…"]
    h = len(items) * 25 + 42
    parts = [_frame(x, y, w, h, title, icon)]
    line_y = y + 40
    for s in items:
        parts += [
            f'<circle cx="{x + 20}" cy="{line_y - 5}" r="2.5" fill="#4c4c4c"/>',
            _text(x + 30, line_y, 14, fit_text(s, 14, w - 48)),
        ]
        line_y += 25
    return "".join(parts), h


def _hitokoto(x: float, y: float, w: float, data: ReportData) -> tuple[str, float]:
    lines = wrap_text(f"“{data.hitokoto}”", 19, w - 30, max_lines=3)
    h = len(lines) * 29 + 34
    parts = [_frame(x, y, w, h, "今日一言", "hitokoto.png")]
    for i, line in enumerate(lines):
        parts.append(
            _text(x + 16, y + 36 + i * 29, 19, line, HITO_PINK, weight="600")
        )
    return "".join(parts), h


def build_report_card(data: ReportData) -> str:
    body: list[str] = []
    gap = 46
    body.append(_header(data))
    y = 210
    body.append(_festivals(25, y, 190, 285, data))
    body.append(_bili(230, y, 323, 285, data))
    y += 285 + gap
    svg, h = _anime(25, y, 528, data)
    body.append(svg)
    y += h + gap
    svg, h = _news_list(25, y, 528, "60S读世界", "60.png", data.six)
    body.append(svg)
    y += h + gap
    svg, h = _news_list(25, y, 528, "IT资讯", "it.png", data.it)
    body.append(svg)
    y += h + gap
    svg, h = _hitokoto(25, y, 528, data)
    body.append(svg)
    y += h + 25

    total_h = y + 15
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{total_h}"'
        f' viewBox="0 0 {W} {total_h}" font-family="sans-serif">'
        f'<rect width="{W}" height="{total_h}" fill="{BG}"/>'
        f'<rect x="15" y="15" width="{W - 30}" height="{total_h - 30}" rx="10"'
        f' fill="{CARD}"/>' + "".join(body) + "</svg>"
    )
