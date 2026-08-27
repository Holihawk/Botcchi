"""权限管理指令：封禁/解封、授权、休眠、功能开关、权限查询。

全部走 PermissionGate 判定，目标用户从 @ 提及或纯数字 QQ 号解析。
"""

import re
from datetime import timedelta

from ..channels.base import Channel, IncomingMessage
from .features import FEATURES
from .gate import SUPERUSER_LEVEL, PermissionGate
from .store import utcnow

_DURATION = re.compile(r"(\d+)\s*(天|小时|时|分钟|分)")
_AT_TOKEN = re.compile(r"@\S+\s*")
_UNIT_MINUTES = {"天": 1440, "小时": 60, "时": 60, "分钟": 1, "分": 1}

_NO_DB = "未配置数据库，权限管理不可用"
_NO_PERM = "你的权限不够哦"


def _parse_target(incoming: IncomingMessage, rest: str) -> tuple[str | None, str]:
    """从 @ 提及或纯数字 token 解析目标用户，返回 (user_id, 剩余文本)"""
    if incoming.mentions:
        return incoming.mentions[0], _AT_TOKEN.sub("", rest, count=1).strip()
    tokens = rest.split()
    for i, token in enumerate(tokens):
        if token.isdigit() and len(token) >= 5:
            return token, " ".join(tokens[:i] + tokens[i + 1 :])
    return None, rest


def _parse_duration(rest: str) -> tuple[timedelta | None, str]:
    """解析「3天 / 12小时 / 30分钟」，无时长表示永久。"""
    m = _DURATION.search(rest)
    if m is None:
        return None, rest
    minutes = int(m.group(1)) * _UNIT_MINUTES[m.group(2)]
    return timedelta(minutes=minutes), (rest[: m.start()] + rest[m.end() :]).strip()


def _duration_text(delta: timedelta | None) -> str:
    if delta is None:
        return "永久"
    minutes = int(delta.total_seconds() // 60)
    if minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


class PermCommands:
    def __init__(self, gate: PermissionGate):
        self.gate = gate

    async def handle(self, incoming: IncomingMessage, channel: Channel) -> bool:
        """尝试作为权限指令处理，返回 True 表示已消费"""
        text = incoming.text.strip()
        is_group = incoming.session.chat_type == "group"

        async def reply(msg: str) -> None:
            await channel.send_text(incoming.session, msg)

        if text == "我的权限" or text == "权限":
            level = await self.gate.level_of(incoming)
            tag = (
                "超级管理员"
                if level >= SUPERUSER_LEVEL
                else {"owner": "群主", "admin": "群管理员"}.get(
                    incoming.sender_role or "", "普通用户"
                )
            )
            await reply(f"{incoming.user_name} 的权限等级：{level}（{tag}）")
            return True

        if text.startswith("权限"):
            target, _ = _parse_target(incoming, text.removeprefix("权限").strip())
            if target is None:
                await reply("用法：权限 @某人")
                return True
            group_id = incoming.session.chat_id if is_group else None
            level = await self.gate.effective_level(target, group_id)
            await reply(f"{target} 的权限等级：{level}")
            return True

        if text == "功能列表":
            if not is_group:
                await reply("功能开关按群生效，请在群聊中查看")
                return True
            state = await self.gate.group_state(incoming.session.chat_id)
            lines = []
            for f in FEATURES.values():
                if f.key in state.su_blocked:
                    mark = "🚫(超管)"
                elif f.key in state.blocked:
                    mark = "🚫"
                else:
                    mark = "✅"
                lines.append(f"{mark} {f.key} — {f.description}")
            sleep = "" if state.active else "\n⚠️ 本群处于休眠中"
            await reply("本群功能：\n" + "\n".join(lines) + sleep)
            return True

        # ---- 以下均为管理操作 -------------------------------------------
        handlers = [
            ("休眠", self._sleep),
            ("唤醒", self._wake),
            ("关闭功能", self._block_feature),
            ("开启功能", self._unblock_feature),
            ("封禁群", self._ban_group),
            ("解封群", self._unban_group),
            ("封禁", self._ban_user),
            ("解封", self._unban_user),
            ("取消授权", self._revoke),
            ("授权", self._grant),
        ]
        for prefix, handler in handlers:
            # startswith 即可：列表按「长前缀在前」排序（封禁群先于封禁）
            if text.startswith(prefix):
                if self.gate.store is None:
                    await reply(_NO_DB)
                    return True
                if not await self.gate.can_manage(incoming):
                    await reply(_NO_PERM)
                    return True
                await handler(
                    incoming, reply, text.removeprefix(prefix).strip()
                )
                return True
        return False

    # ---- 休眠/唤醒 ------------------------------------------------------

    async def _sleep(self, incoming, reply, rest: str) -> None:
        if incoming.session.chat_type != "group":
            await reply("休眠按群生效，请在群聊中使用")
            return
        await self.gate.store.update_group(incoming.session.chat_id, active=False)
        await reply("好，我先睡啦…需要时发送「唤醒」叫我")

    async def _wake(self, incoming, reply, rest: str) -> None:
        if incoming.session.chat_type != "group":
            await reply("休眠按群生效，请在群聊中使用")
            return
        await self.gate.store.update_group(incoming.session.chat_id, active=True)
        await reply("我醒啦！")

    # ---- 功能开关 -------------------------------------------------------

    async def _block_feature(self, incoming, reply, rest: str) -> None:
        await self._toggle_feature(incoming, reply, rest, block=True)

    async def _unblock_feature(self, incoming, reply, rest: str) -> None:
        await self._toggle_feature(incoming, reply, rest, block=False)

    async def _toggle_feature(self, incoming, reply, rest: str, *, block: bool) -> None:
        if incoming.session.chat_type != "group":
            await reply("功能开关按群生效，请在群聊中使用")
            return
        key = rest.strip()
        if key not in FEATURES:
            names = "、".join(FEATURES)
            await reply(f"没有叫「{key}」的功能，可选：{names}")
            return
        group_id = incoming.session.chat_id
        state = await self.gate.group_state(group_id)
        is_su = self.gate.is_superuser(incoming.user_id)
        if block:
            if is_su:
                await self.gate.store.update_group(
                    group_id, su_blocked=set(state.su_blocked) | {key}
                )
            else:
                await self.gate.store.update_group(
                    group_id, blocked=set(state.blocked) | {key}
                )
            await reply(f"已在本群关闭「{key}」")
        else:
            if key in state.su_blocked and not is_su:
                await reply(f"「{key}」是管理者关闭的，我做不了主…")
                return
            await self.gate.store.update_group(
                group_id,
                blocked=set(state.blocked) - {key},
                su_blocked=(
                    set(state.su_blocked) - {key} if is_su else None
                ),
            )
            await reply(f"已在本群开启「{key}」")

    # ---- 封禁 -----------------------------------------------------------

    async def _ban_user(self, incoming, reply, rest: str) -> None:
        target, rest = _parse_target(incoming, rest)
        if target is None:
            await reply("用法：封禁 @某人 [时长如 3天/12小时] [原因]")
            return
        is_group = incoming.session.chat_type == "group"
        is_su = self.gate.is_superuser(incoming.user_id)
        if not is_group and not is_su:
            await reply("全局封禁只有超级管理员可用")
            return
        if self.gate.is_superuser(target):
            await reply("不可以封禁管理者哦")
            return
        group_id = incoming.session.chat_id if is_group else None
        operator_level = await self.gate.level_of(incoming)
        target_level = await self.gate.effective_level(target, group_id)
        if target_level >= operator_level:
            await reply("对方的权限不低于你，封不动…")
            return
        duration, rest = _parse_duration(rest)
        until = utcnow() + duration if duration else None
        await self.gate.store.add_ban(
            target, group_id, until, rest.strip(),
            incoming.user_id, by_superuser=is_su,
        )
        scope = "本群" if is_group else "全局"
        await reply(f"已{scope}封禁 {target}（{_duration_text(duration)}）")

    async def _unban_user(self, incoming, reply, rest: str) -> None:
        target, _ = _parse_target(incoming, rest)
        if target is None:
            await reply("用法：解封 @某人")
            return
        is_group = incoming.session.chat_type == "group"
        is_su = self.gate.is_superuser(incoming.user_id)
        group_id = incoming.session.chat_id if is_group else None
        result = await self.gate.store.remove_ban(target, group_id, allow_su=is_su)
        if result is None and is_su:
            # 超管在群里也可顺手解全局封禁
            result = await self.gate.store.remove_ban(target, None, allow_su=True)
        if result is None:
            await reply(f"{target} 没有被封禁呀")
        elif result is False:
            await reply("这是管理者下的封禁，我解不开…")
        else:
            await reply(f"已解封 {target}")

    async def _ban_group(self, incoming, reply, rest: str) -> None:
        if not self.gate.is_superuser(incoming.user_id):
            await reply("封禁群只有超级管理员可用")
            return
        tokens = rest.split()
        if not tokens or not tokens[0].isdigit():
            await reply("用法：封禁群 <群号> [时长]")
            return
        group_id = tokens[0]
        duration, remainder = _parse_duration(" ".join(tokens[1:]))
        until = utcnow() + duration if duration else None
        await self.gate.store.add_ban(
            None, group_id, until, remainder.strip(),
            incoming.user_id, by_superuser=True,
        )
        await reply(f"已封禁群 {group_id}（{_duration_text(duration)}）")

    async def _unban_group(self, incoming, reply, rest: str) -> None:
        if not self.gate.is_superuser(incoming.user_id):
            await reply("解封群只有超级管理员可用")
            return
        group_id = rest.split()[0] if rest.split() else ""
        if not group_id.isdigit():
            await reply("用法：解封群 <群号>")
            return
        result = await self.gate.store.remove_ban(None, group_id, allow_su=True)
        await reply(
            f"已解封群 {group_id}" if result else f"群 {group_id} 没有被封禁呀"
        )

    # ---- 授权 -----------------------------------------------------------

    async def _grant(self, incoming, reply, rest: str) -> None:
        target, rest = _parse_target(incoming, rest)
        level_token = next((t for t in rest.split() if t.isdigit()), None)
        if target is None or level_token is None:
            await reply("用法：授权 @某人 <等级>，如「授权 @小明 5」")
            return
        level = int(level_token)
        is_group = incoming.session.chat_type == "group"
        is_su = self.gate.is_superuser(incoming.user_id)
        if not is_group and not is_su:
            await reply("全局授权只有超级管理员可用")
            return
        group_id = incoming.session.chat_id if is_group else None
        operator_level = await self.gate.level_of(incoming)
        if level >= operator_level:
            await reply(f"只能授予低于自己等级（{operator_level}）的权限哦")
            return
        if await self.gate.effective_level(target, group_id) >= operator_level:
            await reply("对方的权限不低于你，动不了…")
            return
        await self.gate.store.set_level(target, group_id, level, incoming.user_id)
        scope = "本群" if is_group else "全局"
        await reply(f"已授予 {target} {scope}权限等级 {level}")

    async def _revoke(self, incoming, reply, rest: str) -> None:
        target, _ = _parse_target(incoming, rest)
        if target is None:
            await reply("用法：取消授权 @某人")
            return
        is_group = incoming.session.chat_type == "group"
        is_su = self.gate.is_superuser(incoming.user_id)
        if not is_group and not is_su:
            await reply("全局授权只有超级管理员可用")
            return
        group_id = incoming.session.chat_id if is_group else None
        operator_level = await self.gate.level_of(incoming)
        if (
            not is_su
            and await self.gate.effective_level(target, group_id) >= operator_level
        ):
            await reply("对方的权限不低于你，动不了…")
            return
        removed = await self.gate.store.remove_level(target, group_id)
        await reply(
            f"已取消 {target} 的授权" if removed else f"{target} 没有被授权过呀"
        )
