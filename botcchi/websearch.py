"""联网搜索与网页阅读（Tavily API）。

web_search 出要点式检索结果（含 Tavily 生成的简答），fetch_page 抽取
指定网页正文——两个都走 Tavily，一份 key 全搞定。api.tavily.com 为境外
端点，httpx 默认跟随系统代理环境变量即可直达。
"""

import httpx
from loguru import logger

from .config import WebSearchSettings
from .tools.registry import Tool, tool

API_BASE = "https://api.tavily.com"
SNIPPET_LEN = 300
PAGE_MAX_CHARS = 6000


class WebSearchService:
    def __init__(
        self,
        settings: WebSearchSettings,
        client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=25)
        self.client.headers["Authorization"] = f"Bearer {settings.api_key}"

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _post(self, path: str, payload: dict) -> dict:
        resp = await self.client.post(f"{API_BASE}{path}", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def search(self, query: str, count: int | None = None) -> str:
        count = min(count or self.settings.max_results, 10)
        data = await self._post(
            "/search",
            {
                "query": query,
                "max_results": count,
                "include_answer": "basic",
            },
        )
        results = data.get("results") or []
        if not results:
            return f"没有搜到与「{query}」相关的结果"
        lines = []
        if answer := (data.get("answer") or "").strip():
            lines.append(f"[简答] {answer}\n")
        for i, r in enumerate(results, 1):
            content = " ".join(str(r.get("content", "")).split())
            lines.append(
                f"{i}. {r.get('title', '')}\n   {r.get('url', '')}\n"
                f"   {content[:SNIPPET_LEN]}"
            )
        lines.append(
            "\n（如需某条的完整内容，可用 fetch_page 读取对应 URL）"
        )
        return "\n".join(lines)

    async def fetch(self, url: str) -> str:
        data = await self._post("/extract", {"urls": [url]})
        results = data.get("results") or []
        if not results:
            failed = data.get("failed_results") or []
            reason = failed[0].get("error", "") if failed else ""
            return f"这个网页读不出来（{reason or '可能有反爬或需要登录'}）"
        content = str(results[0].get("raw_content") or "").strip()
        if not content:
            return "网页内容为空或无法解析正文"
        if len(content) > PAGE_MAX_CHARS:
            content = content[:PAGE_MAX_CHARS] + "\n…（内容过长已截断）"
        return content


def build_search_tools(service: WebSearchService) -> list[Tool]:
    @tool(
        name="web_search",
        description=(
            "联网搜索最新信息。用户问到时效性内容（新闻/版本/价格/赛程等）"
            "或你不确定的事实时调用。query 用简洁的关键词或问题，"
            "count 为结果条数（默认 5）。结果附带来源 URL，"
            "回答时可自然引用来源。"
        ),
    )
    async def web_search(query: str, count: int = 0) -> str:
        query = query.strip()
        if not query:
            return "query 不能为空"
        try:
            return await service.search(query, count or None)
        except Exception as e:
            logger.warning(f"联网搜索失败: {e}")
            return "搜索服务暂时不可用，请如实告知用户稍后再试"

    @tool(
        name="fetch_page",
        description=(
            "读取指定网页的正文内容。用户发来链接想让你看看，"
            "或搜索结果需要展开细节时调用。url 需为完整的 http(s) 链接。"
        ),
    )
    async def fetch_page(url: str) -> str:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            return "url 需为完整的 http(s) 链接"
        try:
            return await service.fetch(url)
        except Exception as e:
            logger.warning(f"网页读取失败 {url}: {e}")
            return "网页读取失败（服务不可用或该页拒绝抓取）"

    return [web_search, fetch_page]
