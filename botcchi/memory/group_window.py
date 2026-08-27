"""群聊滑动窗口：记录群内最近 N 条消息（无论是否触发回复），供伪人模式感知语境。

内存实现开箱即用；配置 redis.url 后切换 Redis 实现（重启不丢、多实例共享）。
"""

import json
from abc import ABC, abstractmethod
from collections import defaultdict, deque

from pydantic import BaseModel

from ..channels.base import SessionKey


class WindowEntry(BaseModel):
    user_id: str
    user_name: str
    text: str


class GroupWindow(ABC):
    @abstractmethod
    async def push(self, session: SessionKey, entry: WindowEntry) -> None: ...

    @abstractmethod
    async def recent(self, session: SessionKey) -> list[WindowEntry]: ...


class InMemoryGroupWindow(GroupWindow):
    def __init__(self, size: int = 30):
        self.size = size
        self._data: dict[SessionKey, deque[WindowEntry]] = defaultdict(
            lambda: deque(maxlen=self.size)
        )

    async def push(self, session: SessionKey, entry: WindowEntry) -> None:
        self._data[session].append(entry)

    async def recent(self, session: SessionKey) -> list[WindowEntry]:
        return list(self._data[session])


class RedisGroupWindow(GroupWindow):
    def __init__(self, redis_url: str, size: int = 30):
        import redis.asyncio as aioredis

        self.size = size
        self._redis = aioredis.from_url(redis_url, decode_responses=True)

    def _key(self, session: SessionKey) -> str:
        return f"botcchi:window:{session}"

    async def push(self, session: SessionKey, entry: WindowEntry) -> None:
        key = self._key(session)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.rpush(key, json.dumps(entry.model_dump(), ensure_ascii=False))
            pipe.ltrim(key, -self.size, -1)
            await pipe.execute()

    async def recent(self, session: SessionKey) -> list[WindowEntry]:
        raw = await self._redis.lrange(self._key(session), 0, -1)
        return [WindowEntry.model_validate(json.loads(item)) for item in raw]

    async def aclose(self) -> None:
        await self._redis.aclose()
