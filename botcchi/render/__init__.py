"""SVG → PNG 渲染基建（resvg，无浏览器依赖）。

签到卡片、天气、群总结等需要出图的功能共用：拼 SVG 字符串 → SvgRenderer 出 PNG。
"""

from .cache import RenderCache
from .svg import (
    SvgRenderer,
    esc,
    fit_text,
    image_data_uri,
    text_width,
    to_onebot_file,
    wrap_text,
)

__all__ = [
    "RenderCache",
    "SvgRenderer",
    "esc",
    "fit_text",
    "image_data_uri",
    "text_width",
    "to_onebot_file",
    "wrap_text",
]
