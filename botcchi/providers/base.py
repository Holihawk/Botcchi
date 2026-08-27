"""Provider 抽象：所有提供商适配器实现统一的流式接口"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from ..agent.events import AgentEvent


class ProviderError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ChatProvider(ABC):
    name: str

    @abstractmethod
    def stream_chat(
        self,
        *,
        model: str,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """流式生成。事件序列以 TurnComplete 结尾（其中含拼装好的完整消息）"""
        ...

    async def aclose(self) -> None:
        pass
