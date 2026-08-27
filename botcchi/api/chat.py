"""
网页对话：SSE 流式接口
网页是一个 channel：session_key 为 web:web:<conversation_id>，
会话历史与长期记忆复用 QQ 端同一套存储与提取逻辑
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from ..agent.context import build_system_message, build_user_message
from ..agent.events import (
    ReasoningDelta,
    TextDelta,
    ToolCallEvent,
    ToolResultEvent,
    TurnComplete,
)
from ..agent.messages import ChatMessage
from ..channels.base import IncomingMessage, SessionKey

if TYPE_CHECKING:
    from ..app import AppContext

WEB_USER_ID = "web-user"
"""网页端视为同一个用户（bot 的主人），长期记忆跨网页会话共享"""


class ChatRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1)
    assistant: str | None = None


class ResetRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=64)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def build_chat_router(ctx: "AppContext") -> APIRouter:
    router = APIRouter(prefix="/api")
    service = ctx.service

    async def stream(req: ChatRequest) -> AsyncIterator[str]:
        session = SessionKey(
            channel="web", chat_type="web", chat_id=req.conversation_id
        )
        incoming = IncomingMessage(
            session=session,
            user_id=WEB_USER_ID,
            user_name="你",
            text=req.message,
        )
        if req.assistant:
            assistant = service.assistants.get(req.assistant)
            if assistant is None:
                yield _sse({"type": "error", "message": f"人格不存在: {req.assistant}"})
                return
        else:
            assistant = await service.resolve_assistant(session)

        memory_block = ""
        if service.memory is not None:
            memory_block = service.memory.render_block(
                await service.memory.recall(incoming)
            )
        history = await service.store.get(session)
        messages = [
            build_system_message(assistant, incoming, memory_block),
            *history,
            build_user_message(incoming),
        ]

        reply = ""
        try:
            async for event in service.loop.run(assistant, messages):
                match event:
                    case ReasoningDelta():
                        yield _sse({"type": "reasoning", "text": event.text})
                    case TextDelta():
                        yield _sse({"type": "text", "text": event.text})
                    case ToolCallEvent():
                        yield _sse(
                            {
                                "type": "tool_call",
                                "name": event.name,
                                "arguments": event.arguments,
                            }
                        )
                    case ToolResultEvent():
                        yield _sse(
                            {
                                "type": "tool_result",
                                "name": event.name,
                                "result": event.result[:2000],
                            }
                        )
                    case TurnComplete():
                        reply = event.message.text().strip()
        except Exception as e:
            logger.exception("网页对话失败")
            yield _sse({"type": "error", "message": str(e)})
            return

        if reply:
            await service.store.append(
                session,
                ChatMessage(role="user", content=req.message),
                ChatMessage(role="assistant", content=reply),
            )
            if service.memory is not None and service.memory.should_extract(incoming):
                recent = [*history[-6:], ChatMessage(role="user", content=req.message),
                          ChatMessage(role="assistant", content=reply)]
                task = asyncio.create_task(
                    service.memory.extract_safely(incoming, recent)
                )
                task.add_done_callback(lambda t: t.exception())
        yield _sse({"type": "done", "reply": reply, "assistant": assistant.name})

    @router.post("/chat")
    async def chat(req: ChatRequest) -> StreamingResponse:
        if not req.message.strip():
            raise HTTPException(422, "消息不能为空")
        return StreamingResponse(
            stream(req),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/chat/reset")
    async def reset_chat(req: ResetRequest) -> dict[str, str]:
        session = SessionKey(
            channel="web", chat_type="web", chat_id=req.conversation_id
        )
        await service.store.reset(session)
        return {"status": "ok"}

    return router
