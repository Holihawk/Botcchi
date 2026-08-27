"""上下文构建：system prompt 变量渲染 + 用户消息组装 + 伪人模式群语境"""

from datetime import datetime

from ..channels.base import IncomingMessage
from ..memory.group_window import WindowEntry
from .assistant import Assistant
from .messages import ChatMessage, ContentPart, ImageUrlPart, TextPart

BYM_INSTRUCTION = (
    "你正在这个群里潜水，上面是群里最近的聊天记录。"
    "现在你想接话了：像普通群友一样自然地加入话题，"
    "可以只回应你感兴趣的部分，语气随意。直接输出你要说的话。"
)


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def build_system_message(
    assistant: Assistant,
    incoming: IncomingMessage,
    memory_block: str = "",
    social_block: str = "",
    sticker_block: str = "",
) -> ChatMessage:
    prompt = assistant.prompt.format_map(
        _SafeDict(
            bot_name=assistant.nickname,
            now=datetime.now().strftime("%Y-%m-%d %H:%M"),
            chat_type="群聊" if incoming.session.chat_type == "group" else "私聊",
        )
    )
    return ChatMessage(
        role="system",
        content=prompt + memory_block + social_block + sticker_block,
    )


def build_bym_user_message(
    incoming: IncomingMessage, window: list[WindowEntry]
) -> ChatMessage:
    """以群聊窗口为语境的用户消息"""
    transcript = "\n".join(f"{e.user_name}({e.user_id}): {e.text}" for e in window)
    return ChatMessage(
        role="user",
        content=f"[群聊最近的消息]\n{transcript}\n\n{BYM_INSTRUCTION}",
    )


def build_user_message(incoming: IncomingMessage) -> ChatMessage:
    """组装当前用户消息：群聊带发言人标识（群共享一份对话历史），附图片"""
    text = incoming.text
    if incoming.session.chat_type == "group":
        text = f"[{incoming.user_name}({incoming.user_id})]: {text}"
    if not incoming.images:
        return ChatMessage(role="user", content=text)
    parts: list[ContentPart] = [TextPart(text=text)]
    parts.extend(ImageUrlPart(url=url) for url in incoming.images)
    return ChatMessage(role="user", content=parts)


def persistable_user_message(incoming: IncomingMessage) -> ChatMessage:
    """写入短期历史的用户消息（图片以占位符记录，不持久化二进制内容）。"""
    msg = build_user_message(incoming)
    if isinstance(msg.content, str):
        return msg
    text = msg.text() + " [图片]" * len(incoming.images)
    return ChatMessage(role="user", content=text)
