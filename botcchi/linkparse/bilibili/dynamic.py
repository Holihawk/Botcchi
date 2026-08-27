"""动态与图文（opus）：一次 detail 接口覆盖所有动态类型。

detail 返回的 major 决定了动态的实际内容：视频/直播动态直接转交对应解析器，
图文与转发则就地拼卡片。图片同时可能落在 opus.pics、draw.items 与正文富文本
节点里，三处都要收。

注意：web-dynamic/v1/detail 给的 opus.summary 只是**预览**，长图文会带
has_more=True 被截断。完整正文得再走 opus/detail 接口，那里的
module_content.paragraphs 是分段的全文（文字/图片/代码交替）。
"""

import html
import json

import httpx

from ..base import ContentBlock, ParsedLink, ParseError, format_num, truncate
from . import api, live, video

DETAIL_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"
OPUS_DETAIL_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail"
_IMAGE_REFERER = {"Referer": "https://t.bilibili.com/"}
_FEATURES = (
    "itemOpusStyle,opusBigCover,onlyfansVote,endFooterHidden,"
    "decorationCard,onlyfansAssetsV2,ugcDelete"
)
_OPUS_FEATURES = (
    "onlyfansVote,onlyfansAssetsV2,decorationCard,htmlNewStyle,"
    "ugcDelete,editable,opusPrivateVisible"
)


async def parse_dynamic(client: httpx.AsyncClient, dynamic_id: str) -> ParsedLink:
    data = await api.call(
        client,
        DETAIL_API,
        {
            "id": dynamic_id,
            "timezone_offset": -480,
            "platform": "web",
            "features": _FEATURES,
            "web_location": "333.1368",
        },
        buvid=True,
        wbi=True,
        referer=f"https://t.bilibili.com/{dynamic_id}",
    )
    item = data.get("item")
    if not isinstance(item, dict):
        raise ParseError("动态内容为空")
    return await _from_item(client, item)


async def _from_item(client: httpx.AsyncClient, item: dict) -> ParsedLink:
    """把动态条目转成卡片；视频/直播动态交回专用解析器"""
    major = _major(item)
    major_type = major.get("type")
    if major_type == "MAJOR_TYPE_ARCHIVE" and (
        bvid := (major.get("archive") or {}).get("bvid")
    ):
        return await video.parse_video(client, bvid=bvid)
    if major_type == "MAJOR_TYPE_LIVE_RCMD" and (room_id := _live_room_id(major)):
        return await live.parse_live(client, room_id)

    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    blocks, text = _content_blocks(item)
    # summary 被截断（长图文）时拉 opus/detail 补全整篇正文
    if major_type == "MAJOR_TYPE_OPUS" and _truncated(item):
        if full := await _fetch_opus_blocks(client, item.get("id_str", "")):
            blocks, text = full
    if origin := _origin_summary(item):
        blocks.append(("text", origin))
        text = f"{text}\n{origin}" if text else origin
    opus_title = _title(major)
    title = opus_title or _first_line(text) or f"{author.get('name', '')}的动态"
    has_image = any(kind == "image" for kind, _ in blocks)
    return ParsedLink(
        platform="B站图文" if opus_title else "B站动态",
        title=truncate(title, 60),
        author=author.get("name", ""),
        author_label="UP主",
        stats=_stats(modules.get("module_stat") or {}),
        extra=truncate(text, 120),
        url=_url(item),
        cover=None if has_image else _fallback_cover(major),
        content=blocks,
        content_headers=_IMAGE_REFERER,
        cache_key=f"bili-dyn:{item.get('id_str', '')}",
    )


def _major(item: dict) -> dict:
    module = (item.get("modules") or {}).get("module_dynamic") or {}
    return module.get("major") or {}


def _title(major: dict) -> str:
    """只有专栏式图文带标题，普通动态没有"""
    return (major.get("opus") or {}).get("title") or ""


def _url(item: dict) -> str:
    jump = (item.get("basic") or {}).get("jump_url") or ""
    if jump.startswith("//"):
        return f"https:{jump}"
    return f"https://t.bilibili.com/{item.get('id_str', '')}"


def _stats(stat: dict) -> str:
    def count(key: str) -> str:
        return format_num(int((stat.get(key) or {}).get("count") or 0))

    return f"点赞 {count('like')} · 评论 {count('comment')} · 转发 {count('forward')}"


def _content(item: dict) -> tuple[str, list[str]]:
    """动态正文与配图（纯文本 + 图片列表，转发摘要等场景用）"""
    blocks, text = _content_blocks(item)
    return text, [v for kind, v in blocks if kind == "image"]


def _content_blocks(item: dict) -> tuple[list[ContentBlock], str]:
    """把动态正文拆成有序块（文字段落与图片按原文交替）+ 纯文本摘要。

    富文本节点里的内嵌图片（VIEW_PICTURE）就地切断上一段文字，长图文因此能
    还原「一段文字→图→一段文字」的排版；opus.pics/draw 里的尾部图集接在末尾。
    已内联的图片不再重复计入尾部。
    """
    module = (item.get("modules") or {}).get("module_dynamic") or {}
    major = module.get("major") or {}
    opus = major.get("opus") or {}
    # 转发动态自己的文案在 desc，原创动态的在 opus.summary
    summary = module.get("desc") or opus.get("summary") or {}

    blocks: list[ContentBlock] = []
    plain: list[str] = []
    seen: set[str] = set()
    buf: list[str] = []

    def flush() -> None:
        joined = "".join(buf).strip()
        buf.clear()
        if joined:
            blocks.append(("text", joined))

    for node in summary.get("rich_text_nodes") or []:
        if node.get("type") == "RICH_TEXT_NODE_TYPE_VIEW_PICTURE":
            flush()
            for pic in node.get("pics") or []:
                if (src := pic.get("src")) and src not in seen:
                    seen.add(src)
                    blocks.append(("image", src))
            continue
        if text := node.get("text"):
            buf.append(text)
            plain.append(text)
    flush()

    if not any(kind == "text" for kind, _ in blocks) and (t := summary.get("text")):
        blocks.insert(0, ("text", t.strip()))
        plain.append(t)

    draw = major.get("draw") or {}
    trailing = [pic["url"] for pic in opus.get("pics") or [] if pic.get("url")]
    trailing += [
        pic["src"]
        for pic in draw.get("items") or draw.get("pictures") or []
        if pic.get("src")
    ]
    for src in trailing:
        if src not in seen:
            seen.add(src)
            blocks.append(("image", src))

    return blocks, "".join(plain).strip()


def _truncated(item: dict) -> bool:
    """opus.summary 是否被截断（长图文才需要再拉完整正文）"""
    module = (item.get("modules") or {}).get("module_dynamic") or {}
    summary = (module.get("major") or {}).get("opus", {}).get("summary") or {}
    return bool(summary.get("has_more"))


async def _fetch_opus_blocks(
    client: httpx.AsyncClient, opus_id: str
) -> tuple[list[ContentBlock], str] | None:
    """走 opus/detail 接口取整篇正文的有序块；失败返回 None（回退到 summary）"""
    if not opus_id:
        return None
    try:
        data = await api.call(
            client,
            OPUS_DETAIL_API,
            {"id": opus_id, "timezone_offset": -480, "features": _OPUS_FEATURES},
            buvid=True,
            wbi=True,
            referer=f"https://www.bilibili.com/opus/{opus_id}",
        )
    except Exception:
        return None
    modules = (data.get("item") or {}).get("modules") or []
    content = next(
        (m["module_content"] for m in modules if m.get("module_content")), None
    )
    if not content:
        return None
    blocks, text = _paragraphs_to_blocks(content.get("paragraphs") or [])
    return (blocks, text) if blocks else None


def _paragraphs_to_blocks(
    paragraphs: list[dict],
) -> tuple[list[ContentBlock], str]:
    """opus 分段正文 → 有序块：连续文字段并成一块，遇图片切开（文字/图交替）。

    para_type：1=文字（nodes 里 word/rich）、2=图片、7=代码块；其余（分割线、
    投票卡等）跳过。
    """
    blocks: list[ContentBlock] = []
    plain: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        joined = "\n".join(b for b in buf if b.strip()).strip()
        buf.clear()
        if joined:
            blocks.append(("text", joined))

    for para in paragraphs:
        para_type = para.get("para_type")
        if para_type == 1:
            words = _para_text(para.get("text") or {})
            if words.strip():
                buf.append(words.strip())
                plain.append(words)
        elif para_type == 7:
            code = (para.get("code") or {}).get("content") or ""
            if code.strip():
                buf.append(html.unescape(code).strip())
        elif para_type == 2:
            flush()
            for pic in (para.get("pic") or {}).get("pics") or []:
                if url := pic.get("url"):
                    blocks.append(("image", url))
    flush()
    return blocks, "".join(plain).strip()


def _para_text(text_node: dict) -> str:
    """文字段落 nodes 拼回纯文本（word 普通字 / rich 富文本，含表情/链接文案）"""
    parts: list[str] = []
    for node in text_node.get("nodes") or []:
        if word := node.get("word"):
            parts.append(word.get("words") or "")
        elif rich := node.get("rich"):
            parts.append(rich.get("text") or "")
    return "".join(parts)


def _fallback_cover(major: dict) -> str | None:
    """没有配图时用被转内容的封面兜底"""
    for key in ("archive", "live_rcmd"):
        if cover := (major.get(key) or {}).get("cover"):
            return cover
    return None


def _first_line(text: str) -> str:
    return next((line for line in text.splitlines() if line.strip()), "")


def _live_room_id(major: dict) -> str:
    """直播动态的房间信息塞在 content 字段的 JSON 字符串里"""
    content = (major.get("live_rcmd") or {}).get("content") or ""
    try:
        info = json.loads(content).get("live_play_info") or {}
    except (json.JSONDecodeError, AttributeError):
        return ""
    return str(info.get("room_id") or "")


def _origin_summary(item: dict) -> str:
    """转发动态附上被转内容的一句话摘要"""
    if item.get("type") != "DYNAMIC_TYPE_FORWARD":
        return ""
    orig = item.get("orig")
    if not isinstance(orig, dict) or not orig.get("visible"):
        return "（原动态已失效）"
    author = ((orig.get("modules") or {}).get("module_author") or {}).get("name", "")
    major = _major(orig)
    if archive := major.get("archive"):
        body = f"视频《{archive.get('title', '')}》"
    else:
        text, _ = _content(orig)
        body = _title(major) or truncate(text, 60) or "动态"
    return f"转发 @{author}：{body}"
