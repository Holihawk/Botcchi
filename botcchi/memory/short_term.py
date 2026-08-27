"""短期对话历史存储。

M1 用进程内实现；接口保持异步，M2 换 PostgreSQL/Redis 背书的实现不动调用方。
"""

from abc import ABC, abstractmethod
from collections import defaultdict

from ..agent.messages import ChatMessage
from ..channels.base import SessionKey


class ConversationStore(ABC):
    @abstractmethod
    async def get(self, session: SessionKey) -> list[ChatMessage]: ...

    @abstractmethod
    async def append(self, session: SessionKey, *messages: ChatMessage) -> None: ...

    @abstractmethod
    async def reset(self, session: SessionKey) -> None: ...


class InMemoryConversationStore(ConversationStore):
    def __init__(self, max_messages: int = 40):
        self.max_messages = max_messages
        self._data: dict[SessionKey, list[ChatMessage]] = defaultdict(list)

    async def get(self, session: SessionKey) -> list[ChatMessage]:
        return list(self._data[session])

    async def append(self, session: SessionKey, *messages: ChatMessage) -> None:
        history = self._data[session]
        history.extend(messages)
        if len(history) > self.max_messages:
            del history[: len(history) - self.max_messages]

    async def reset(self, session: SessionKey) -> None:
        self._data.pop(session, None)
