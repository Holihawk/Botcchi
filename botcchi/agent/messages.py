"""对话消息模型：channel 无关，序列化为各 provider 协议在 provider 层完成"""

from typing import Literal

from pydantic import BaseModel

Role = Literal["system", "user", "assistant", "tool"]


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImageUrlPart(BaseModel):
    type: Literal["image_url"] = "image_url"
    url: str
    """http(s) 地址或 data:image/...;base64 数据"""


ContentPart = TextPart | ImageUrlPart


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str = ""
    """原始 JSON 字符串（流式增量拼接的结果）"""


class ChatMessage(BaseModel):
    role: Role
    content: str | list[ContentPart] = ""
    reasoning: str | None = None
    """思维链内容，仅本地保留，不回传给 API"""
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    def text(self) -> str:
        """提取纯文本内容。"""
        if isinstance(self.content, str):
            return self.content
        return "".join(p.text for p in self.content if isinstance(p, TextPart))


def system(text: str) -> ChatMessage:
    return ChatMessage(role="system", content=text)


def user(content: str | list[ContentPart]) -> ChatMessage:
    return ChatMessage(role="user", content=content)


def assistant(text: str) -> ChatMessage:
    return ChatMessage(role="assistant", content=text)


def tool_result(call_id: str, result: str) -> ChatMessage:
    return ChatMessage(role="tool", content=result, tool_call_id=call_id)


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


__all__ = [
    "ChatMessage",
    "ContentPart",
    "ImageUrlPart",
    "Role",
    "TextPart",
    "ToolCall",
    "Usage",
    "assistant",
    "system",
    "tool_result",
    "user",
]
