"""测试基建：可编程的 mock OpenAI 兼容 SSE 服务，经 ASGITransport 注入 httpx。"""

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def text_chunks(text: str, *, reasoning: str | None = None) -> list[dict[str, Any]]:
    """把一段文本拆成多个 SSE delta 块（模拟真实流式）。"""
    chunks = []
    if reasoning:
        chunks.append({"choices": [{"delta": {"reasoning_content": reasoning}}]})
    for i in range(0, len(text), 5):
        chunks.append({"choices": [{"delta": {"content": text[i : i + 5]}}]})
    return chunks


def tool_call_chunks(name: str, arguments: str, call_id: str = "call_1") -> list[dict[str, Any]]:
    """模拟工具调用的增量下发（id/name 先到，arguments 分片）。"""
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "function": {"name": name, "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        }
    ]
    for i in range(0, len(arguments), 4):
        chunks.append(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": arguments[i : i + 4]}}
                            ]
                        }
                    }
                ]
            }
        )
    return chunks


class MockOpenAI:
    """按顺序弹出脚本化响应；记录收到的每个请求体。"""

    def __init__(self):
        self.scripts: list[list[dict[str, Any]] | int] = []
        self.requests: list[dict[str, Any]] = []
        self.auth_headers: list[str] = []
        self.app = FastAPI()
        self.app.post("/v1/chat/completions")(self._endpoint)

    def enqueue(self, chunks: list[dict[str, Any]]) -> None:
        self.scripts.append(chunks)

    def enqueue_status(self, status_code: int) -> None:
        self.scripts.append(status_code)

    async def _endpoint(self, request: Request):
        self.requests.append(await request.json())
        self.auth_headers.append(request.headers.get("authorization", ""))
        assert self.scripts, "mock 服务收到了未编排的请求"
        script = self.scripts.pop(0)
        if isinstance(script, int):
            return JSONResponse({"error": "scripted"}, status_code=script)

        async def stream():
            for chunk in script:
                yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app))


@pytest.fixture
def mock_openai() -> MockOpenAI:
    return MockOpenAI()
