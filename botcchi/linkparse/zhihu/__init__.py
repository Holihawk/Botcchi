"""知乎解析：专栏文章、问题、回答。

- 文章 `zhuanlan.zhihu.com/p/{id}`、问题 `zhihu.com/question/{id}` 走桌面
  `www.zhihu.com/api/v4/*`，需 zse96 签名（免登录，d_c0 传空即可，见 sign.py）。
- 回答 `.../answer/{id}` 桌面接口要真实设备 d_c0（JS 生成，拿不到），改走
  移动端 `api.zhihu.com/answers/{id}` + 匿名 OAuth token（业界周知的公开只读
  令牌），无需签名。只做信息摘要卡，正文取纯文本节选、封面取首图。
"""

import html
import re
from urllib.parse import quote

import httpx

from ..base import (
    PC_UA,
    ParsedLink,
    ParseError,
    Pattern,
    format_num,
    truncate,
)
from .sign import sign_fetch

_ARTICLE = re.compile(r"zhuanlan\.zhihu\.com/p/(?P<id>\d+)")
_ANSWER = re.compile(
    r"(?:www\.)?zhihu\.com/(?:question/(?P<qid>\d+)/)?answer/(?P<aid>\d+)"
)
_QUESTION = re.compile(r"(?:www\.)?zhihu\.com/question/(?P<id>\d+)(?![/\d])")

PATTERNS: list[Pattern] = [
    ("zhuanlan.zhihu.com/p", _ARTICLE),
    ("zhihu.com/answer", _ANSWER),
    ("/answer/", _ANSWER),
    ("zhihu.com/question", _QUESTION),
]

_ARTICLE_API = (
    "https://www.zhihu.com/api/v4/articles/{}"
    "?include=content,voteup_count,comment_count,excerpt,ip_info,author"
)
_QUESTION_API = (
    "https://www.zhihu.com/api/v4/questions/{}"
    "?include=detail,excerpt,visit_count,answer_count,follower_count,"
    "comment_count,author"
)
_ANSWER_API = "https://api.zhihu.com/answers/{}?include={}"
_ANSWER_INCLUDE = quote(
    "content,excerpt,voteup_count,comment_count,thanks_count,updated_time,"
    "question,author,ip_info"
)
# 知乎安卓客户端公开的匿名只读 OAuth 令牌，免登录访问移动端接口
_ZERO_TOKEN = "oauth c3cef7c66a1843f8b3a9e6a1e3160e20"
_ANDROID_UA = (
    "com.zhihu.android/Futureve/6.60.0 Mozilla/5.0 (Linux; Android 12) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Mobile Safari/537.36"
)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_IMG = re.compile(
    r'<img[^>]+(?:data-original|data-actualsrc|src)="(https://[^"]+?\.(?:jpe?g|png|webp|gif)[^"]*)"'
)


def _text(raw: str | None) -> str:
    """把知乎富文本 HTML 压成纯文本节选。"""
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", raw or ""))).strip()


def _first_image(*htmls: str | None) -> str | None:
    for raw in htmls:
        if raw and (m := _IMG.search(raw)):
            return m.group(1).replace("\\", "")
    return None


async def parse(client: httpx.AsyncClient, match: re.Match[str]) -> ParsedLink:
    groups = match.groupdict()
    if "aid" in groups:
        return await _parse_answer(client, groups["aid"])
    if match.re is _ARTICLE:
        return await _parse_article(client, groups["id"])
    return await _parse_question(client, groups["id"])


async def _fetch_web(client: httpx.AsyncClient, url: str) -> dict:
    resp = await client.get(
        url,
        headers={
            "User-Agent": PC_UA,
            "Referer": "https://www.zhihu.com/",
            **sign_fetch(url),
        },
    )
    if resp.status_code != 200:
        raise ParseError(f"知乎接口返回 {resp.status_code}（可能被风控或内容不存在）")
    return resp.json()


async def _parse_article(client: httpx.AsyncClient, article_id: str) -> ParsedLink:
    data = await _fetch_web(client, _ARTICLE_API.format(article_id))
    author = data.get("author") or {}
    stats = (
        f"赞同 {format_num(int(data.get('voteup_count') or 0))} · "
        f"评论 {format_num(int(data.get('comment_count') or 0))}"
    )
    if ip := data.get("ip_info"):
        stats += f" · IP {ip}"
    return ParsedLink(
        platform="知乎文章",
        title=truncate(data.get("title") or "（无标题）", 60),
        author=author.get("name", ""),
        stats=stats,
        extra=truncate(_text(data.get("excerpt")), 90),
        url=f"https://zhuanlan.zhihu.com/p/{article_id}",
        cover=data.get("title_image")
        or _first_image(data.get("content"), data.get("excerpt")),
        cache_key=f"zhihu:article:{article_id}",
    )


async def _parse_question(client: httpx.AsyncClient, question_id: str) -> ParsedLink:
    data = await _fetch_web(client, _QUESTION_API.format(question_id))
    author = data.get("author") or {}
    return ParsedLink(
        platform="知乎问题",
        title=truncate(data.get("title") or "（无标题）", 60),
        author=author.get("name", ""),
        author_label="提问",
        stats=(
            f"{format_num(int(data.get('answer_count') or 0))} 回答 · "
            f"{format_num(int(data.get('follower_count') or 0))} 关注 · "
            f"浏览 {format_num(int(data.get('visit_count') or 0))}"
        ),
        extra=truncate(_text(data.get("detail") or data.get("excerpt")), 90),
        url=f"https://www.zhihu.com/question/{question_id}",
        cover=_first_image(data.get("detail")),
        cache_key=f"zhihu:question:{question_id}",
    )


async def _parse_answer(client: httpx.AsyncClient, answer_id: str) -> ParsedLink:
    resp = await client.get(
        _ANSWER_API.format(answer_id, _ANSWER_INCLUDE),
        headers={
            "User-Agent": _ANDROID_UA,
            "Authorization": _ZERO_TOKEN,
            "x-api-version": "3.0.76",
        },
    )
    if resp.status_code != 200:
        raise ParseError(f"知乎回答接口返回 {resp.status_code}（可能被风控或已删除）")
    data = resp.json()
    question = data.get("question") or {}
    author = data.get("author") or {}
    stats = (
        f"赞同 {format_num(int(data.get('voteup_count') or 0))} · "
        f"评论 {format_num(int(data.get('comment_count') or 0))} · "
        f"感谢 {format_num(int(data.get('thanks_count') or 0))}"
    )
    qid = question.get("id") or ""
    return ParsedLink(
        platform="知乎回答",
        title=truncate(question.get("title") or "（无标题）", 60),
        author=author.get("name", ""),
        author_label="答主",
        stats=stats,
        extra=truncate(_text(data.get("excerpt") or data.get("content")), 90),
        url=(
            f"https://www.zhihu.com/question/{qid}/answer/{answer_id}"
            if qid
            else f"https://www.zhihu.com/answer/{answer_id}"
        ),
        cover=_first_image(data.get("content")),
        cache_key=f"zhihu:answer:{answer_id}",
    )
