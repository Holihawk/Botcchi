"""触发引擎：决定一条消息是否唤起 agent。

@必回 / 唤醒词必回 / 私聊开关 / 群随机概率（伪人模式）/ 会话冷却。
"""

import random
import time

from ..channels.base import IncomingMessage, SessionKey
from ..config import TriggerSettings


class TriggerEngine:
    def __init__(self, settings: TriggerSettings):
        self.settings = settings
        self._last_reply: dict[SessionKey, float] = {}

    def should_reply(self, incoming: IncomingMessage) -> bool:
        if incoming.session.chat_type == "web":
            return True
        if incoming.session.chat_type == "private":
            return self.settings.private_chat
        if incoming.at_me:
            return True
        if self.is_wake(incoming.text):
            return True
        if self._in_cooldown(incoming.session):
            return False
        return random.random() < self.settings.group_reply_rate

    def is_wake(self, text: str) -> bool:
        """消息是否点名唤醒（含唤醒词 = 直接召唤，与 @ 同等对待）"""
        return any(w and w in text for w in self.settings.wake_words)

    def is_bare_wake(self, text: str) -> bool:
        """仅包含唤醒词（允许常见标点）时，视为单独召唤"""
        normalized = text.strip(" \t\r\n,，。.!！?？~～")
        return bool(normalized) and normalized in self.settings.wake_words

    def mark_replied(self, session: SessionKey) -> None:
        self._last_reply[session] = time.monotonic()

    def _in_cooldown(self, session: SessionKey) -> bool:
        if self.settings.cooldown_seconds <= 0:
            return False
        last = self._last_reply.get(session)
        return last is not None and (
            time.monotonic() - last < self.settings.cooldown_seconds
        )
