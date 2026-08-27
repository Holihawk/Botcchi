"""群聊空召唤的快速问候，不经过模型"""

import random
import time

from .agent.assistant import GreetingPolicy
from .channels.base import Channel, IncomingMessage
from .stickers import StickerService


class GreetingService:
    EMOTIONS = ("打招呼", "吃喝", "惊讶")

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self._last: dict[str, float] = {}
        self._recent: dict[str, str] = {}

    def can_greet(self, incoming: IncomingMessage, policy: GreetingPolicy) -> bool:
        if not policy.enabled or incoming.session.chat_type != "group":
            return False
        key = str(incoming.session)
        last = self._last.get(key)
        return last is None or time.monotonic() - last >= policy.cooldown_seconds

    def choose(self, session_key: str, policy: GreetingPolicy) -> str:
        replies = [r for r in policy.replies if r.strip()] or ["呜...后藤在呢"]
        previous = self._recent.get(session_key)
        choices = [r for r in replies if r != previous] or replies
        result = self.rng.choice(choices)
        self._recent[session_key] = result
        self._last[session_key] = time.monotonic()
        return result

    def last_text(self, session_key: str) -> str | None:
        return self._recent.get(session_key)

    async def greet(
        self,
        incoming: IncomingMessage,
        channel: Channel,
        policy: GreetingPolicy,
        stickers: StickerService | None,
    ) -> bool:
        if not self.can_greet(incoming, policy):
            return False
        text = self.choose(str(incoming.session), policy)
        await channel.send_text(incoming.session, text)
        if stickers is not None:
            await stickers.send_random(channel, incoming.session, self.EMOTIONS)
        return True
