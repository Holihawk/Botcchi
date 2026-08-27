"""天气卡片：纯矢量图标（零图片资产）+ 冷暖双主题的紧凑排版。

渲染压力控制：无外部图片、无滤镜模糊，图标全部为基础形状，
逻辑宽 420，高度随预警条数浮动。
"""

from dataclasses import dataclass
from datetime import date

from ..render import esc, fit_text, text_width
from . import AQI_COLORS, DailyForecast, WeatherData

W = 420


@dataclass(frozen=True, slots=True)
class Theme:
    bg_top: str
    bg_bottom: str
    accent: str
    text: str
    subtext: str
    panel: str
    """预报行底色"""


WARM = Theme(
    bg_top="#FFF8EE", bg_bottom="#FFE2BE", accent="#F0813C",
    text="#5A4630", subtext="#A38763", panel="#FFFFFF",
)
COLD = Theme(
    bg_top="#EDF4FC", bg_bottom="#CFE2F6", accent="#4E8FD9",
    text="#37485C", subtext="#7C93AB", panel="#FFFFFF",
)


def icon_kind(icon: str) -> str:
    """和风图标代码 → 图标类别。"""
    try:
        code = int(icon)
    except ValueError:
        return "cloudy"
    if code in (100, 150):
        return "sunny"
    if code in (101, 102, 103, 151, 152, 153):
        return "cloudy"
    if code == 104:
        return "overcast"
    if 302 <= code <= 304:
        return "thunder"
    if 300 <= code <= 399:
        return "rain"
    if 400 <= code <= 499:
        return "snow"
    if 500 <= code <= 515:
        return "fog"
    return "cloudy"


def resolve_theme(icon: str, temp: int) -> Theme:
    """晴/多云且不冷 → 暖色调；降水/阴/雾或低温 → 冷色调"""
    if icon_kind(icon) in ("sunny", "cloudy") and temp >= 10:
        return WARM
    return COLD


# ---- 矢量图标 ------------------------------------------------------------


def _sun(cx: float, cy: float, r: float, color: str) -> str:
    rays = "".join(
        f'<line x1="{cx}" y1="{cy - r * 1.25}" x2="{cx}" y2="{cy - r * 1.55}" '
        f'stroke="{color}" stroke-width="{r * 0.22}" stroke-linecap="round" '
        f'transform="rotate({a} {cx} {cy})"/>'
        for a in range(0, 360, 45)
    )
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>{rays}'


def _cloud(cx: float, cy: float, r: float, color: str) -> str:
    return (
        f'<circle cx="{cx - r * 0.55}" cy="{cy}" r="{r * 0.62}" fill="{color}"/>'
        f'<circle cx="{cx + r * 0.1}" cy="{cy - r * 0.32}" r="{r * 0.75}" fill="{color}"/>'
        f'<circle cx="{cx + r * 0.7}" cy="{cy + r * 0.05}" r="{r * 0.55}" fill="{color}"/>'
        f'<rect x="{cx - r * 0.9}" y="{cy + r * 0.1}" width="{r * 1.9}" '
        f'height="{r * 0.5}" rx="{r * 0.25}" fill="{color}"/>'
    )


def _drops(cx: float, cy: float, r: float, color: str, n: int = 3) -> str:
    step = r * 0.7
    x0 = cx - step * (n - 1) / 2
    return "".join(
        f'<line x1="{x0 + i * step}" y1="{cy}" x2="{x0 + i * step - r * 0.18}" '
        f'y2="{cy + r * 0.55}" stroke="{color}" stroke-width="{r * 0.18}" '
        f'stroke-linecap="round"/>'
        for i in range(n)
    )


def weather_icon(kind: str, cx: float, cy: float, size: float, theme: Theme) -> str:
    """在 (cx, cy) 画一个宽约 size 的天气图标。"""
    r = size * 0.32
    sun_color = "#F5B841" if theme is WARM else "#E8B64C"
    cloud_color = "#FFFFFF" if theme is COLD else "#FDF3E3"
    cloud_edge = theme.accent
    if kind == "sunny":
        # 光芒延伸到盘径的 1.55 倍，压到 0.85r 才不至于比云团大一圈
        return _sun(cx, cy, r * 0.85, sun_color)
    if kind == "cloudy":
        return (
            _sun(cx - r * 0.7, cy - r * 0.5, r * 0.7, sun_color)
            + _cloud(cx + r * 0.2, cy + r * 0.25, r, cloud_color)
        )
    if kind == "overcast":
        return _cloud(cx, cy, r * 1.05, cloud_color)
    if kind == "rain":
        return _cloud(cx, cy - r * 0.25, r, cloud_color) + _drops(
            cx, cy + r * 0.75, r, theme.accent
        )
    if kind == "thunder":
        # 居中的锯齿闪电，从云底探出：x 跨度对称于 cx，避免整体偏右下
        bolt = (
            f'<polygon points="{cx + r * 0.22},{cy + r * 0.15} '
            f'{cx - r * 0.32},{cy + r * 0.82} {cx + r * 0.02},{cy + r * 0.82} '
            f'{cx - r * 0.22},{cy + r * 1.55} {cx + r * 0.36},{cy + r * 0.6} '
            f'{cx + r * 0.04},{cy + r * 0.6}" fill="#F5B841"/>'
        )
        return _cloud(cx, cy - r * 0.3, r, cloud_color) + bolt
    if kind == "snow":
        flakes = "".join(
            f'<circle cx="{cx - r * 0.7 + i * r * 0.7}" cy="{cy + r * 1.0}" '
            f'r="{r * 0.14}" fill="{theme.accent}"/>'
            for i in range(3)
        )
        return _cloud(cx, cy - r * 0.25, r, cloud_color) + flakes
    # fog
    return "".join(
        f'<rect x="{cx - r * 1.1}" y="{cy - r * 0.6 + i * r * 0.55}" '
        f'width="{r * 2.2}" height="{r * 0.22}" rx="{r * 0.11}" '
        f'fill="{cloud_edge}" opacity="{0.85 - i * 0.2}"/>'
        for i in range(3)
    )


# ---- 卡片 ----------------------------------------------------------------

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _day_label(index: int, fx_date: str) -> str:
    if index == 0:
        return "今天"
    if index == 1:
        return "明天"
    try:
        d = date.fromisoformat(fx_date)
        return _WEEKDAYS[d.weekday()]
    except ValueError:
        return "后天"


def _daily_row(
    d: DailyForecast, index: int, y: float, theme: Theme,
    lo: int, hi: int,
) -> str:
    label = _day_label(index, d.date)
    text = d.text_day if d.text_day == d.text_night else f"{d.text_day}转{d.text_night}"
    # 温度区间条：三天全局归一化
    bar_x0, bar_x1 = 240, 344
    span = max(hi - lo, 1)
    x_min = bar_x0 + (d.temp_min - lo) / span * (bar_x1 - bar_x0)
    x_max = bar_x0 + (d.temp_max - lo) / span * (bar_x1 - bar_x0)
    return f"""
  <rect x="16" y="{y}" width="{W - 32}" height="48" rx="12"
        fill="{theme.panel}" opacity="0.55"/>
  <text x="30" y="{y + 24}" font-size="15" font-weight="600" fill="{theme.text}">{label}</text>
  <text x="30" y="{y + 40}" font-size="10" fill="{theme.subtext}">{esc(d.date[5:])}</text>
  <g>{weather_icon(icon_kind(d.icon), 106, y + 24, 34, theme)}</g>
  <text x="136" y="{y + 29}" font-size="13" fill="{theme.text}">{esc(fit_text(text, 13, 96))}</text>
  <text x="{bar_x0 - 8}" y="{y + 29}" font-size="12" text-anchor="end"
        fill="{theme.subtext}">{d.temp_min}°</text>
  <line x1="{bar_x0}" y1="{y + 24}" x2="{bar_x1}" y2="{y + 24}"
        stroke="{theme.panel}" stroke-width="5" stroke-linecap="round" opacity="0.8"/>
  <line x1="{x_min}" y1="{y + 24}" x2="{x_max}" y2="{y + 24}"
        stroke="{theme.accent}" stroke-width="5" stroke-linecap="round"/>
  <text x="{bar_x1 + 8}" y="{y + 29}" font-size="12" fill="{theme.text}"
        font-weight="600">{d.temp_max}°</text>"""


def build_weather_card(data: WeatherData) -> str:
    theme = resolve_theme(data.icon, data.temp)
    warn_h = len(data.warnings) * 34
    height = 234 + warn_h + len(data.daily) * 56 + 16

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" '
        f'viewBox="0 0 {W} {height}" font-family="sans-serif">',
        f"""<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="{theme.bg_top}"/>
    <stop offset="1" stop-color="{theme.bg_bottom}"/>
  </linearGradient></defs>
  <rect width="{W}" height="{height}" rx="20" fill="url(#bg)"/>""",
        # 头部：城市 + 观测时间 + AQI
        f"""<text x="24" y="42" font-size="24" font-weight="700"
        fill="{theme.text}">{esc(fit_text(data.city, 24, 240))}</text>
  <text x="24" y="62" font-size="11" fill="{theme.subtext}">观测时间 {esc(data.obs_time)}</text>""",
    ]
    if data.aqi:
        color = AQI_COLORS.get(data.aqi_category, "#8A9BAD")
        tag = f"AQI {data.aqi} {data.aqi_category}"
        tw = text_width(tag, 12) + 20
        parts.append(
            f'<rect x="{W - 24 - tw}" y="28" width="{tw}" height="24" rx="12" '
            f'fill="{color}"/>'
            f'<text x="{W - 24 - tw / 2}" y="44" font-size="12" fill="#FFFFFF" '
            f'text-anchor="middle">{esc(tag)}</text>'
        )
    # 当前天气
    temp_text = f"{data.temp}°"
    parts.append(
        f"""<text x="24" y="136" font-size="58" font-weight="700"
        fill="{theme.text}">{temp_text}</text>
  <g>{weather_icon(icon_kind(data.icon), text_width(temp_text, 58) + 62, 116, 62, theme)}</g>
  <text x="24" y="166" font-size="15" fill="{theme.text}" font-weight="600">{esc(data.text)}</text>
  <text x="24" y="188" font-size="12" fill="{theme.subtext}">体感 {data.feels_like}° ｜ 湿度 {esc(data.humidity)}% ｜ {esc(data.wind_dir)} {esc(data.wind_scale)}级</text>"""
    )
    y = 204.0
    for title in data.warnings:
        parts.append(
            f'<rect x="16" y="{y}" width="{W - 32}" height="28" rx="8" '
            f'fill="#D96161" opacity="0.14"/>'
            f'<text x="28" y="{y + 19}" font-size="12" fill="#C24A4A">'
            f"⚠ {esc(fit_text(title, 12, W - 70))}</text>"
        )
        y += 34
    y += 14
    lo = min((d.temp_min for d in data.daily), default=0)
    hi = max((d.temp_max for d in data.daily), default=1)
    for i, d in enumerate(data.daily):
        parts.append(_daily_row(d, i, y, theme, lo, hi))
        y += 56
    parts.append("</svg>")
    return "".join(parts)
