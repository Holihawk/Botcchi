"""商店货架卡片：清爽粉色单主题，按商品数动态高度。

挂件商品直接用其贴图当缩略图；消耗品画轻量矢量图标（无额外素材）。
卡片内容只由货架与用户持有状态决定（无时间戳），同一状态可命中出图缓存。
"""

from .card import (
    F_CR105,
    F_JCYT,
    F_RXK,
    F_SH,
    PINK,
    PINK_DARK,
    W,
    _asset_uri,
    _defs,
    _glass,
    _sparkle,
    _text,
)
from .shop import ShelfEntry

SUB = "#C78BA5"

ROW_H = 104
ROW_GAP = 14
TOP = 128


def _background(h: int) -> str:
    blobs = [
        (130, 70, 90, "#ff9ec3", 0.55),
        (820, 100, 85, "#d9b8ff", 0.45),
        (500, h - 60, 100, "#ffc9de", 0.6),
    ]
    parts = [f'<rect width="{W}" height="{h}" fill="url(#bg-grad)"/>']
    parts += [
        f'<circle cx="{x}" cy="{y}" r="{r}" fill="{c}" fill-opacity="{o}"'
        f' filter="url(#blob-blur)"/>'
        for x, y, r, c, o in blobs
    ]
    parts += [_sparkle(60, h - 80, 8, 0.85), _sparkle(880, 60, 10, 0.85)]
    return "".join(parts)


def _icon(entry: ShelfEntry, x: float, y: float) -> str:
    """64x64 商品缩略图。"""
    g = entry.goods
    accent = PINK_DARK
    if g.pendant is not None:
        return (
            f'<image x="{x}" y="{y}" width="64" height="64"'
            f' href="{_asset_uri(g.pendant.asset)}"/>'
        )
    if g.id == "double_card":
        # 一张斜放的卡片 + ×2
        return (
            f'<g transform="rotate(-12 {x + 32} {y + 32})">'
            f'<rect x="{x + 12}" y="{y + 8}" width="40" height="52" rx="6"'
            f' fill="#ffffff" fill-opacity="0.9" stroke="{accent}"'
            f' stroke-width="2.5"/>'
            + _text(x + 32, y + 42, 20, F_SH, "×2", accent,
                    anchor="middle", weight="700")
            + "</g>"
        )
    # 入场券：两端带缺口的票根
    return (
        f'<g transform="rotate(-8 {x + 32} {y + 32})">'
        f'<rect x="{x + 4}" y="{y + 16}" width="56" height="32" rx="5"'
        f' fill="#ffffff" fill-opacity="0.9" stroke="{accent}"'
        f' stroke-width="2.5"/>'
        f'<circle cx="{x + 4}" cy="{y + 32}" r="5" fill="{accent}"/>'
        f'<circle cx="{x + 60}" cy="{y + 32}" r="5" fill="{accent}"/>'
        f'<line x1="{x + 42}" y1="{y + 20}" x2="{x + 42}" y2="{y + 44}"'
        f' stroke="{accent}" stroke-width="2" stroke-dasharray="3,3"/>'
        + _text(x + 24, y + 40, 16, F_SH, "券", accent, anchor="middle",
                weight="700")
        + "</g>"
    )


def build_shop_card(entries: list[ShelfEntry]) -> str:
    """货架卡：不含金币余额等每人每天都变的内容，保证缓存命中率"""
    h = TOP + len(entries) * (ROW_H + ROW_GAP) + 26
    accent = PINK_DARK
    stops = (
        '<stop offset="0" stop-color="#FFD9E8"/>'
        '<stop offset="0.55" stop-color="#FBE4E4"/>'
        '<stop offset="1" stop-color="#FFF3F7"/>'
    )
    parts = [
        _background(h),
        _glass(22, 22, W - 44, h - 44, 26, 0.42, 0.75, shadow=True),
        _text(W / 2, 88, 38, F_CR105, "波奇の商店", PINK, anchor="middle"),
        _text(W / 2, 114, 15, F_JCYT,
              "「购买 商品名」下单 ·「佩戴 挂件名」戴上 ·「我的背包」看余额",
              SUB, anchor="middle"),
    ]
    for i, e in enumerate(entries):
        y = TOP + i * (ROW_H + ROW_GAP)
        parts.append(_glass(42, y, W - 84, ROW_H, 18, 0.35, 0.7))
        parts.append(_icon(e, 60, y + 20))
        parts.append(_text(148, y + 42, 24, F_JCYT, e.goods.name, PINK))
        parts.append(_text(148, y + 76, 16, F_RXK, e.goods.desc, SUB))
        # 右侧：价格 + 状态徽章
        parts.append(
            _text(W - 68, y + 46, 24, F_RXK, f"{e.goods.price} 金币", accent,
                  anchor="end")
        )
        badge = None
        if e.equipped:
            badge = "佩戴中"
        elif e.owned:
            badge = f"已拥有 ×{e.owned}"
        elif e.goods.one_time:
            badge = "限购一次"
        if badge:
            bw = len(badge) * 14 + 24
            parts.append(
                f'<rect x="{W - 68 - bw}" y="{y + 60}" width="{bw}" height="26"'
                f' rx="13" fill="{accent}" fill-opacity="0.18"'
                f' stroke="{accent}" stroke-opacity="0.5" stroke-width="1"/>'
                + _text(W - 68 - bw / 2, y + 79, 14, F_SH, badge, accent,
                        anchor="middle")
            )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}"'
        f' viewBox="0 0 {W} {h}" font-family="sans-serif">'
        + _defs(stops)
        + "".join(parts)
        + "</svg>"
    )
