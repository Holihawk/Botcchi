"""Agent 流式事件：provider 与 agent loop 对外统一的输出协议"""

from pydantic import BaseModel

from .messages import ChatMessage, Usage


class TextDelta(BaseModel):
    text: str


class ReasoningDelta(BaseModel):
    text: str


class ToolCallEvent(BaseModel):
    """一次完整的工具调用请求（流式增量已在 provider 层拼装完成）"""

    id: str
    name: str
    arguments: str


class ToolResultEvent(BaseModel):
    id: str
    name: str
    result: str


class TurnComplete(BaseModel):
    """一轮模型生成结束，附带拼装好的完整 assistant 消息"""

    message: ChatMessage
    usage: Usage | None = None


AgentEvent = TextDelta | ReasoningDelta | ToolCallEvent | ToolResultEvent | TurnComplete

__all__ = [
    "AgentEvent",
    "ReasoningDelta",
    "TextDelta",
    "ToolCallEvent",
    "ToolResultEvent",
    "TurnComplete",
    "Usage",
]
