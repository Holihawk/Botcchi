"""OpenAI 兼容 API 的流式适配器。

覆盖 DeepSeek / 硅基流动 / 阿里百炼 / 火山引擎 / Gemini(OpenAI 兼容端点) 等。
思维链通过原生 reasoning_content 字段分离，取代旧版的正则剥离。
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from loguru import logger

from ..agent.events import (
    AgentEvent,
    ReasoningDelta,
    TextDelta,
    ToolCallEvent,
    TurnComplete,
)
from ..agent.messages import ChatMessage, ToolCall, Usage
from ..config import ProviderSettings
from .base import ChatProvider, ProviderError
from .keypool import KeyPool

MAX_KEY_ATTEMPTS = 3


def build_http_client(settings: ProviderSettings) -> httpx.AsyncClient:
    """按 provider 的代理策略构建 HTTP 客户端。

    httpx 默认跟随 http(s)_proxy 环境变量；proxy=false 强制直连，
    避免本地代理未运行时 LLM 请求全部 ConnectError。
    """
    timeout = httpx.Timeout(120.0)
    if settings.proxy is None or settings.proxy is True:
        return httpx.AsyncClient(timeout=timeout)
    if settings.proxy is False:
        return httpx.AsyncClient(timeout=timeout, trust_env=False)
    return httpx.AsyncClient(
        timeout=timeout, proxy=settings.proxy, trust_env=False
    )


def message_to_openai(msg: ChatMessage) -> dict[str, Any]:
    data: dict[str, Any] = {"role": msg.role}
    if isinstance(msg.content, str):
        data["content"] = msg.content
    else:
        parts: list[dict[str, Any]] = []
        for part in msg.content:
            if part.type == "text":
                parts.append({"type": "text", "text": part.text})
            else:
                parts.append({"type": "image_url", "image_url": {"url": part.url}})
        data["content"] = parts
    if msg.tool_calls:
        data["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments or "{}"},
            }
            for c in msg.tool_calls
        ]
    if msg.role == "tool":
        data["tool_call_id"] = msg.tool_call_id
    return data


class OpenAICompatProvider(ChatProvider):
    def __init__(
        self,
        settings: ProviderSettings,
        client: httpx.AsyncClient | None = None,
    ):
        self.name = settings.name
        self.api_base = settings.api_base.rstrip("/")
        self.extra_headers = settings.extra_headers
        self.keypool = KeyPool(settings.api_keys)
        self._client = client or build_http_client(settings)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stream_chat(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[AgentEvent]:
        body: dict[str, Any] = {
            "model": model,
            "stream": True,
            "messages": [message_to_openai(m) for m in messages],
        }
        if tools:
            body["tools"] = tools
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        attempts = min(MAX_KEY_ATTEMPTS, self.keypool.size)
        last_error: ProviderError | None = None
        for _ in range(attempts):
            key = self.keypool.acquire()
            try:
                async for event in self._stream_once(body, key):
                    yield event
                return
            except ProviderError as e:
                if e.status_code == 429:
                    logger.warning(f"[{self.name}] key 限速，退避 15 分钟后换 key 重试")
                    self.keypool.penalize(key)
                    last_error = e
                    continue
                raise
        raise last_error or ProviderError("所有 key 均不可用", status_code=429)

    async def _stream_once(
        self, body: dict[str, Any], api_key: str
    ) -> AsyncIterator[AgentEvent]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            **self.extra_headers,
        }
        url = f"{self.api_base}/chat/completions"

        content_buf: list[str] = []
        reasoning_buf: list[str] = []
        tool_calls_buf: dict[int, dict[str, str]] = {}
        usage: Usage | None = None

        async with self._client.stream(
            "POST", url, json=body, headers=headers
        ) as resp:
            if resp.status_code != 200:
                detail = (await resp.aread()).decode("utf-8", errors="replace")
                raise ProviderError(
                    f"[{self.name}] HTTP {resp.status_code}: {detail[:500]}",
                    status_code=resp.status_code,
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    logger.warning(f"[{self.name}] 无法解析的 SSE 块: {payload[:200]}")
                    continue
                if raw_usage := chunk.get("usage"):
                    usage = Usage(
                        prompt_tokens=raw_usage.get("prompt_tokens", 0),
                        completion_tokens=raw_usage.get("completion_tokens", 0),
                        total_tokens=raw_usage.get("total_tokens", 0),
                    )
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}

                if reasoning := (
                    delta.get("reasoning_content") or delta.get("reasoning")
                ):
                    reasoning_buf.append(reasoning)
                    yield ReasoningDelta(text=reasoning)
                if text := delta.get("content"):
                    content_buf.append(text)
                    yield TextDelta(text=text)
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    buf = tool_calls_buf.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.get("id"):
                        buf["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        buf["name"] = fn["name"]
                    if fn.get("arguments"):
                        buf["arguments"] += fn["arguments"]

        tool_calls = [
            ToolCall(id=b["id"] or f"call_{i}", name=b["name"], arguments=b["arguments"])
            for i, b in sorted(tool_calls_buf.items())
        ]
        for call in tool_calls:
            yield ToolCallEvent(id=call.id, name=call.name, arguments=call.arguments)

        yield TurnComplete(
            message=ChatMessage(
                role="assistant",
                content="".join(content_buf),
                reasoning="".join(reasoning_buf) or None,
                tool_calls=tool_calls or None,
            ),
            usage=usage,
        )
