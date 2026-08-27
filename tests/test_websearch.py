"""联网搜索（Tavily）与记忆工具。"""

import httpx
import pytest
from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.config import WebSearchSettings
from botcchi.websearch import WebSearchService, build_search_tools


def make_service(handler) -> WebSearchService:
    settings = WebSearchSettings(api_key="tvly-test")
    return WebSearchService(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def tavily_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == "Bearer tvly-test"
    if request.url.path == "/search":
        return httpx.Response(200, json={
            "answer": "波奇是吉他英雄。",
            "results": [
                {"title": "孤独摇滚 Wiki", "url": "https://w.example/bocchi",
                 "content": "后藤一里，吉他手。  多余空白  "},
                {"title": "新闻", "url": "https://n.example/1", "content": "x" * 500},
            ],
        })
    if request.url.path == "/extract":
        return httpx.Response(200, json={
            "results": [{"url": "https://w.example/bocchi",
                         "raw_content": "正文内容" * 3}],
        })
    return httpx.Response(404)


async def test_search_and_fetch():
    svc = make_service(tavily_handler)
    out = await svc.search("波奇")
    assert "[简答] 波奇是吉他英雄。" in out
    assert "1. 孤独摇滚 Wiki" in out and "https://w.example/bocchi" in out
    assert "x" * 301 not in out  # 摘要截断
    assert "fetch_page" in out
    assert "正文内容" in await svc.fetch("https://w.example/bocchi")
    await svc.aclose()


async def test_search_tools_and_failure():
    svc = make_service(tavily_handler)
    web_search, fetch_page = build_search_tools(svc)
    assert "孤独摇滚" in await web_search.execute('{"query": "波奇"}', None)
    assert "query 不能为空" in await web_search.execute('{"query": " "}', None)
    assert "完整的 http" in await fetch_page.execute('{"url": "w.example"}', None)
    await svc.aclose()

    bad = make_service(lambda r: httpx.Response(500))
    out = await build_search_tools(bad)[0].execute('{"query": "x"}', None)
    assert "暂时不可用" in out
    await bad.aclose()


# ---- 记忆工具 --------------------------------------------------------------


@pytest.fixture
async def mem_ctx(mock_openai, tmp_path):
    from botcchi.app import build_context

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    context = build_context(settings, client=mock_openai.client())
    await context.db.init()
    yield context
    await context.shutdown()


def msg(chat_type: str = "group") -> IncomingMessage:
    chat_id = "777" if chat_type == "group" else "10001"
    return IncomingMessage(
        session=SessionKey(channel="fake", chat_type=chat_type, chat_id=chat_id),
        user_id="10001",
        user_name="hhk",
        text="",
    )


async def test_memory_tools(mem_ctx):
    from botcchi.agent.messages import ToolCall
    from botcchi.tools.registry import ToolContext

    tools = mem_ctx.service.loop.tools
    ctx = ToolContext(incoming=msg(), channel=FakeChannel())

    async def call(name, args):
        return await tools.execute(
            ToolCall(id="1", name=name, arguments=args), ctx
        )

    out = await call("save_memory", '{"content": "hhk 的生日是 3 月 5 日"}')
    assert "已记住" in out
    assert "已经记过了" in await call(
        "save_memory", '{"content": "hhk 的生日是 3 月 5 日"}'
    )
    await call("save_memory", '{"content": "本群周五晚排练", "scope": "group"}')

    out = await call("search_memory", '{"keyword": "生日"}')
    assert "3 月 5 日" in out and "排练" not in out
    out = await call("search_memory", "{}")
    assert "生日" in out and "排练" in out  # 群里检索并入群维度

    # 私聊场景查不到群维度的记忆
    private = ToolContext(incoming=msg("private"), channel=FakeChannel())
    out = await tools.execute(
        ToolCall(id="2", name="search_memory", arguments="{}"), private
    )
    assert "生日" in out and "排练" not in out
