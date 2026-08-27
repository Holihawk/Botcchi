"""指令注册表：把「文本 → 处理函数」从 ChatService 的巨型 if 链解放出来。

各功能模块提供 build_xxx_commands(...) -> list[Command]，ChatService 构造时
按顺序汇总注册——匹配按注册顺序进行，长前缀须先注册（如「总结定时取消」在
「总结定时」之前）。handler 返回 False 表示"看过但不消费"，消息继续走后续
流程直至落回闲聊（如「帮助我…」）。

功能开关在 dispatch 入口统一拦截（沿用 perms.features 的指令前缀归组）。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .channels.base import Channel, IncomingMessage
from .perms import PermissionGate, feature_for_command

Handler = Callable[[IncomingMessage, Channel, str], Awaitable[bool]]
"""(incoming, channel, rest) -> 是否消费；rest 为去掉指令前缀后的剩余文本"""

Matcher = Callable[[str], str | None]
"""text -> rest；返回 None 表示未命中"""


@dataclass(slots=True)
class Command:
    match: Matcher
    handler: Handler


def exact(*names: str, lower: bool = False) -> Matcher:
    """全文等于任一名字才命中，rest 恒为空串"""

    def matcher(text: str) -> str | None:
        probe = text.lower() if lower else text
        return "" if probe in names else None

    return matcher


def prefix(*prefixes: str) -> Matcher:
    """前缀命中，rest 为去前缀后 strip 的剩余部分"""

    def matcher(text: str) -> str | None:
        for p in prefixes:
            if text.startswith(p):
                return text.removeprefix(p).strip()
        return None

    return matcher


class CommandRouter:
    def __init__(self, perms: PermissionGate):
        self.perms = perms
        self._commands: list[Command] = []

    def add(self, *commands: Command) -> None:
        self._commands.extend(commands)

    async def dispatch(
        self, incoming: IncomingMessage, channel: Channel
    ) -> bool:
        """尝试作为内置指令处理，返回 True 表示已消费"""
        text = incoming.text.strip()
        if not text:
            return False
        # 功能开关：被禁用功能的指令统一在入口拦截
        feature = feature_for_command(text)
        if (
            feature
            and incoming.session.chat_type == "group"
            and await self.perms.feature_blocked(
                incoming.session.chat_id, feature
            )
        ):
            await channel.send_text(
                incoming.session, f"「{feature}」在本群已被关闭"
            )
            return True
        for cmd in self._commands:
            rest = cmd.match(text)
            if rest is None:
                continue
            if await cmd.handler(incoming, channel, rest):
                return True
        return False
