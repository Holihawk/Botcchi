"""百度贴吧解析：tieba.baidu.com/p/{tid} 帖子。

web 页 403，参照 nonebot-plugin-parser-lite 走移动端 protobuf 接口
`tiebac.baidu.com/c/f/pb/page`（免登录、无签名，multipart 上传 protobuf
请求体）。`.desc` 为预编译的 FileDescriptorSet，用 google.protobuf 动态解析，
直接读消息字段取主楼信息，不引用参照的 msgspec 模型层。

protobuf 为按需依赖：import 与 .desc 加载都延迟到真正解析时，缺依赖只让贴吧
这一家降级，不影响其它平台。
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from ..base import (
    PC_UA,
    Media,
    ParsedLink,
    ParseError,
    Pattern,
    format_num,
    truncate,
)

_POST = re.compile(r"tieba\.baidu\.com/p/(?P<tid>\d+)")

PATTERNS: list[Pattern] = [("tieba.baidu.com/p/", _POST)]

_API = "http://tiebac.baidu.com/c/f/pb/page"
_BOUNDARY = "-*_r1999"
_REF_HEADERS = {"User-Agent": PC_UA, "Referer": "https://tieba.baidu.com/"}

# PbContent.type → 语义（见参照 models.py 的 Contents.from_tbdata）
_TEXT_TYPES = {0, 9, 18, 27, 40}  # 纯文本/电话/话题/百科/梗百科
_EMOJI_TYPES = {2, 11}
_IMAGE_TYPES = {3, 20}
_AT_TYPE = 4
_LINK_TYPE = 1


@lru_cache(maxsize=2)
def _message(name: str):
    """按需从 .desc 加载 protobuf 消息类（延迟 import，缺依赖时抛出）"""
    from google.protobuf import descriptor_pb2, descriptor_pool
    from google.protobuf.message_factory import GetMessageClass

    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString((Path(__file__).parent / f"{name}.desc").read_bytes())
    pool = descriptor_pool.DescriptorPool()
    for fd in fds.file:
        pool.Add(fd)
    return GetMessageClass(pool.FindMessageTypeByName(name))


def _build_req(tid: int) -> bytes:
    req = _message("PbPageReqIdl")()
    req.data.common._client_type = 2
    req.data.common._client_version = "12.64.1.1"
    req.data.kz = tid
    req.data.pn = 1
    req.data.rn = 30
    req.data.r = 0
    req.data.lz = 0
    req.data.with_floor = 1
    req.data.floor_sort_type = 1
    req.data.floor_rn = 4
    return req.SerializeToString()


async def parse(client: httpx.AsyncClient, match: re.Match[str]) -> ParsedLink:
    tid = match.group("tid")
    try:
        payload = _build_req(int(tid))
    except ImportError as e:  # 未安装 protobuf 依赖
        raise ParseError("贴吧解析需要 protobuf 依赖（未安装）") from e

    body = (
        (
            f'--{_BOUNDARY}\r\nContent-Disposition: form-data; name="data"; '
            f'filename="file"\r\n\r\n'
        ).encode()
        + payload
        + f"\r\n--{_BOUNDARY}--\r\n".encode()
    )
    resp = await client.post(
        _API,
        params={"cmd": 302001},
        headers={
            "x_bd_data_type": "protobuf",
            "User-Agent": "miku/39",
            "Host": "tiebac.baidu.com",
            "Content-Type": f"multipart/form-data; boundary={_BOUNDARY}",
        },
        content=body,
    )
    resp.raise_for_status()

    res = _message("PbPageResIdl")()
    res.ParseFromString(resp.content)
    if res.error.errorno:
        raise ParseError(f"贴吧接口: {res.error.errmsg or res.error.errorno}")

    thread = res.data.thread
    if not (thread.id or thread.title):
        raise ParseError("贴吧帖子数据为空（可能已删除或被限制）")

    author = thread.author
    excerpt, images = _content(res)
    media, cover = _media(thread, images)
    fname = res.data.forum.name or thread.fname

    return ParsedLink(
        platform=f"贴吧·{fname}吧" if fname else "百度贴吧",
        title=truncate(thread.title or excerpt or "（无标题）", 60),
        author=author.name_show or author.name,
        author_label="楼主",
        stats=_stats(thread),
        extra=truncate(excerpt, 90),
        url=f"https://tieba.baidu.com/p/{tid}",
        cover=cover,
        cover_referer="https://tieba.baidu.com/" if cover else None,
        media=media,
        cache_key=f"tieba:{tid}",
    )


def _fragments(res: Any) -> list:
    """主楼内容碎片：优先 post_list 首楼，退回 thread.first_post_content"""
    if res.data.post_list:
        return list(res.data.post_list[0].content)
    return list(res.data.thread.first_post_content)


def _content(res: Any) -> tuple[str, list[str]]:
    texts: list[str] = []
    images: list[str] = []
    for frag in _fragments(res):
        t = frag.type
        if t in _TEXT_TYPES or t in _EMOJI_TYPES or t == _LINK_TYPE:
            texts.append(frag.text)
        elif t == _AT_TYPE:
            texts.append(f"@{frag.text}")
        elif t in _IMAGE_TYPES and frag.origin_src:
            images.append(frag.origin_src)
    return " ".join("".join(texts).split()), images


def _media(thread: Any, images: list[str]) -> tuple[Media | None, str | None]:
    video = thread.video_info
    if video.video_url:
        return (
            Media(
                kind="video",
                url=video.video_url,
                headers=_REF_HEADERS,
                duration=int(video.video_duration or 0),
            ),
            video.thumbnail_url or None,
        )
    if images:
        # 图集转发时封面即重复，交给 send_card 跳过
        return Media(kind="images", urls=images, headers=_REF_HEADERS), None
    return None, None


def _stats(thread: Any) -> str:
    parts = []
    if thread.view_num:
        parts.append(f"浏览 {format_num(thread.view_num)}")
    if agree := thread.agree.agree_num:
        parts.append(f"点赞 {format_num(agree)}")
    parts.append(f"回复 {format_num(thread.reply_num)}")
    if thread.share_num:
        parts.append(f"分享 {format_num(thread.share_num)}")
    return " · ".join(parts)
