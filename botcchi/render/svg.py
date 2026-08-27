"""SVG 渲染器与拼装辅助。

resvg 支持 SVG 1.1 静态子集：渐变、蒙版、feGaussianBlur/feDropShadow 滤镜、
内嵌 data URI 位图，足够做卡片；不支持 CSS backdrop-filter，毛玻璃效果用
「模糊色块打底 + 半透明白面板」模拟。
"""

import asyncio
import base64
from xml.sax.saxutils import escape

import resvg_py

from ..config import RenderSettings


def esc(text: object) -> str:
    """转义进入 SVG 文本/属性的用户输入"""
    return escape(str(text), {'"': "&quot;", "'": "&apos;"})


def text_width(text: str, size: float) -> float:
    """估算文本宽度：CJK 全宽、其余按 0.55 字宽"""
    return sum(size if ord(c) > 0x2E7F else size * 0.55 for c in text)


def fit_text(text: str, size: float, max_w: float) -> str:
    """超宽截断并加省略号"""
    if text_width(text, size) <= max_w:
        return text
    while text and text_width(text + "…", size) > max_w:
        text = text[:-1]
    return text + "…"


def wrap_text(
    text: str, size: float, max_w: float, max_lines: int = 2
) -> list[str]:
    """按估算宽度逐字换行，超出行数上限截断"""
    lines: list[str] = []
    cur = ""
    for ch in text:
        if text_width(cur + ch, size) > max_w and cur:
            lines.append(cur)
            cur = ""
        cur += ch
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = fit_text(lines[-1] + "…", size, max_w)
    return lines


def image_data_uri(data: bytes, mime: str = "image/png") -> str:
    """位图 → data URI，供 SVG <image href> 内嵌（resvg 支持 png/jpeg/gif）"""
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def to_onebot_file(png: bytes) -> str:
    """PNG → OneBot image 段的 base64 file 形式"""
    return f"base64://{base64.b64encode(png).decode()}"


class SvgRenderer:
    def __init__(self, settings: RenderSettings | None = None):
        self.settings = settings or RenderSettings()

    def to_png(self, svg: str, *, zoom: float | None = None) -> bytes:
        """同步渲染（CPU 密集，约几十毫秒）；异步路径用 render()"""
        family = self.settings.font_family
        return bytes(
            resvg_py.svg_to_bytes(
                svg_string=svg,
                zoom=zoom or self.settings.zoom,
                font_family=family,
                sans_serif_family=family,
                serif_family=family,
                font_dirs=self.settings.font_dirs or None,
            )
        )

    async def render(self, svg: str, *, zoom: float | None = None) -> bytes:
        return await asyncio.to_thread(self.to_png, svg, zoom=zoom)
