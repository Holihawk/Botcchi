"""复读机：群友的本质。

连续 trigger_count 条相同内容（文本 + 图片指纹）后按概率跟读一次；
小概率改为「打断施法！」。每轮只跟一次，内容变化即重新计数。
移植自旧版 fudu.py；图片判定用 OneBot image 段的 file 指纹，无需下载。
"""

import random
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal

from ..config import RepeatSettings

MAX_GROUPS = 500

RepeatAction = Literal["repeat", "break"]


def content_key(text: str, image_keys: list[str]) -> str:
    """复读判定用的内容指纹（文本 + 图片 file 指纹，同旧版 text|-|hash 结构）"""
    return f"{text}|-|{'|'.join(image_keys)}"


@dataclass(slots=True)
class _GroupState:
    key: str = ""
    count: int = 0
    repeated: bool = False


@dataclass
class Repeater:
    settings: RepeatSettings
    _groups: OrderedDict = field(default_factory=OrderedDict)

    def feed(self, group_key: str, key: str) -> RepeatAction | None:
        """喂入一条群消息的内容指纹，返回动作（跟读/打断），无动作返回 None"""
        if not self.settings.enabled or not key.strip("|-"):
            return None
        state = self._state(group_key)
        if key == state.key:
            state.count += 1
        else:
            state.key = key
            state.count = 1
            state.repeated = False

        if state.count < self.settings.trigger_count or state.repeated:
            return None
        state.repeated = True
        if random.random() >= self.settings.probability:
            return None
        if random.random() < self.settings.break_probability:
            state.key = ""
            state.count = 0
            return "break"
        return "repeat"

    def break_text(self) -> str:
        return random.choice(self.settings.break_texts or ["打断施法！"])

    def _state(self, key: str) -> _GroupState:
        if key in self._groups:
            self._groups.move_to_end(key)
            return self._groups[key]
        while len(self._groups) >= MAX_GROUPS:
            self._groups.popitem(last=False)
        state = _GroupState()
        self._groups[key] = state
        return state
