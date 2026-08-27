"""API key 池：轮换使用，被限速的 key 退避一段时间。

设计回收自旧版 services/llm 的 KeyStatusStore + hitori TokenCounter。
"""

import time


class KeyPool:
    def __init__(self, keys: list[str]):
        if not keys:
            raise ValueError("key 池不能为空")
        self._blocked_until: dict[str, float] = dict.fromkeys(keys, 0.0)
        self._last_used: dict[str, float] = dict.fromkeys(keys, 0.0)

    def acquire(self) -> str:
        """取一个 key：优先可用的、最久未用的；全部被退避时取最早解封的"""
        now = time.monotonic()
        available = [k for k, t in self._blocked_until.items() if t <= now]
        if available:
            key = min(available, key=lambda k: self._last_used[k])
        else:
            key = min(self._blocked_until, key=lambda k: self._blocked_until[k])
        self._last_used[key] = now
        return key

    def penalize(self, key: str, seconds: float = 900.0) -> None:
        """限速退避（默认 15 分钟，对齐旧版行为）"""
        if key in self._blocked_until:
            self._blocked_until[key] = time.monotonic() + seconds

    @property
    def size(self) -> int:
        return len(self._blocked_until)
