"""群管理：进群欢迎、退群提醒、迎新文案管理与消息撤回。

本轮不做 bot 作为群管理员的操作（禁言/踢人等）；撤回只针对
bot 自己发的消息（引用 + 「撤回」，仅超管）。
"""

from loguru import logger
from sqlalchemy import select

from .channels.base import Channel, IncomingMessage, SessionKey
from .db.engine import Database
from .db.models import GroupGreetingRow
from .perms import PermissionGate

DEFAULT_WELCOME = "欢、欢迎新朋友…！有问题可以直接问群友哦～"
DEFAULT_FAREWELL = "{名字} 悄悄地离开了群聊…"
DEFAULT_KICKED = "{名字} 被 {操作者} 送走了."
"""沿旧版文案；拿不到操作者时退化为 DEFAULT_KICKED_NO_OP"""
DEFAULT_KICKED_NO_OP = "{名字} 被请出了群聊"

FEATURE_GREETING = "迎新"


class GreetingStore:
    def __init__(self, db: Database):
        self.db = db

    async def get(self, group_id: str) -> tuple[str | None, str | None]:
        async with self.db.session() as session:
            row = (
                await session.execute(
                    select(GroupGreetingRow).where(
                        GroupGreetingRow.group_id == group_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None, None
            return row.welcome, row.farewell

    async def set(
        self,
        group_id: str,
        *,
        welcome: str | None = ...,  # type: ignore[assignment]
        farewell: str | None = ...,  # type: ignore[assignment]
    ) -> None:
        """省略的字段保持不变；显式传 None 表示恢复默认"""
        async with self.db.session() as session:
            row = (
                await session.execute(
                    select(GroupGreetingRow).where(
                        GroupGreetingRow.group_id == group_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = GroupGreetingRow(group_id=group_id)
                session.add(row)
            if welcome is not ...:
                row.welcome = welcome
            if farewell is not ...:
                row.farewell = farewell
            await session.commit()


class GroupAdminService:
    def __init__(self, perms: PermissionGate, store: GreetingStore | None = None):
        self.perms = perms
        self.store = store

    # ---- 进群/退群事件 --------------------------------------------------

    async def on_member_change(
        self,
        channel: Channel,
        group_id: str,
        user_id: str,
        nickname: str,
        change: str,
        operator_name: str = "",
    ) -> None:
        state = await self.perms.group_state(group_id)
        if not state.active or FEATURE_GREETING in state.all_blocked:
            return
        if await self.perms.is_banned(user_id, group_id):
            return
        welcome, farewell = (
            await self.store.get(group_id) if self.store else (None, None)
        )
        session = SessionKey(channel=channel.name, chat_type="group", chat_id=group_id)
        if change == "join":
            await channel.send_text(
                session, welcome or DEFAULT_WELCOME, at_user=user_id
            )
            return
        name = nickname or user_id
        if farewell is not None:
            template = farewell
        elif change == "kick":
            template = DEFAULT_KICKED if operator_name else DEFAULT_KICKED_NO_OP
        else:
            template = DEFAULT_FAREWELL
        await channel.send_text(
            session,
            template.replace("{名字}", name).replace(
                "{操作者}", operator_name or "管理员"
            ),
        )
        logger.info(f"群 {group_id} 成员变动（{change}）: {user_id}")

    # ---- 指令 -----------------------------------------------------------

    async def handle_command(
        self, incoming: IncomingMessage, channel: Channel
    ) -> bool:
        text = incoming.text.strip()
        is_group = incoming.session.chat_type == "group"

        async def reply(msg: str) -> None:
            await channel.send_text(incoming.session, msg)

        # 撤回：引用一条消息 + 「撤回」（引用块会前置到 text，按末行匹配）
        lines = text.splitlines()
        if lines and lines[-1].strip() == "撤回" and (
            len(lines) == 1 or incoming.reply_to_id
        ):
            if not self.perms.is_superuser(incoming.user_id):
                await reply("哼，我不撤")
                return True
            if not incoming.reply_to_id:
                await reply("回复要撤回的那条消息，再发「撤回」")
                return True
            try:
                ok = await channel.recall(incoming.reply_to_id)
            except NotImplementedError:
                await reply("当前聊天渠道不支持撤回")
                return True
            if not ok:
                await reply("只能撤回自己的且不太久远的消息")
            return True

        if text.startswith(("设置欢迎语", "设置退群提示")):
            field = "welcome" if text.startswith("设置欢迎语") else "farewell"
            label = "欢迎语" if field == "welcome" else "退群提示"
            if not is_group:
                await reply(f"{label}按群设置，请在群聊中使用")
                return True
            if not await self.perms.can_manage(incoming):
                await reply(f"只有管理者能设置{label}哦")
                return True
            if self.store is None:
                await reply("未配置数据库，迎新文案不可保存")
                return True
            content = (
                text.removeprefix("设置欢迎语").removeprefix("设置退群提示").strip()
            )
            if not content:
                hint = "" if field == "welcome" else "，可用 {名字} 代表退群的人"
                await reply(f"用法：设置{label} <文案>{hint}；填「默认」恢复默认")
                return True
            value = None if content == "默认" else content
            group_id = incoming.session.chat_id
            if field == "welcome":
                await self.store.set(group_id, welcome=value)
            else:
                await self.store.set(group_id, farewell=value)
            await reply(
                f"已恢复默认{label}" if value is None else f"{label}已更新：{content}"
            )
            return True

        if text == "查看欢迎语":
            if not is_group:
                await reply("欢迎语按群设置，请在群聊中查看")
                return True
            welcome, farewell = (
                await self.store.get(incoming.session.chat_id)
                if self.store
                else (None, None)
            )
            await reply(
                "本群迎新文案：\n"
                f"欢迎语{'（默认）' if welcome is None else ''}："
                f"{welcome or DEFAULT_WELCOME}\n"
                f"退群提示{'（默认）' if farewell is None else ''}："
                f"{farewell or DEFAULT_FAREWELL}"
            )
            return True

        return False
