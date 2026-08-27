"""点赞：「赞我」给用户资料卡点赞；用户回赞 bot 时聚合致谢并发金币/好感度。

迁移自旧版 builtin_plugins/likes.py：批量点赞（每批 10 个、失败即停，QQ 自身
限额生效），回赞走 30 秒聚合窗口（满 10 个提前结算），奖励数值沿用旧版。
"""

import asyncio
import random
from dataclasses import dataclass, field

from loguru import logger

from ..channels.base import Channel, SessionKey
from ..db.archive import MessageArchive
from .service import SocialService

LIKE_BATCH = 10
MAX_BATCHES = 5
"""普通号每日 10 个，SVIP 更多；逐批尝试直到失败"""
THANKS_THRESHOLD = 10

GIVEN_TEXTS = [
    "已经给你点了 {count} 个赞! 记得给{bot}回赞哦~",
    "点完啦，一共 {count} 个。感觉自己今天也帮上忙了呢",
    "已经给你点了 {count} 个赞! 希望你今天也能顺顺利利的",
    "给你点了 {count} 个赞! 收好哦~",
    "已经给你点了{count}个赞!\n>///< {bot}希望你也能给她点赞哦~",
]
ALREADY_TEXTS = [
    "今天已经给你点过赞啦... ♡",
    "今天已经给你点过赞啦，贪心是不行的哦",
    "今、今天的社交份额已经到极限了...",
    "今天给你点过赞了哦... ♡´･ᴗ･`♡",
]
NOT_FRIEND_TEXTS = [
    "那...那个...我们还不是好友，{bot}只敢给好友点赞啦...",
    "加个好友嘛，成为好友{bot}才能给你点赞哦~",
]
UNKNOWN_FAIL_TEXTS = [
    "点不动...可能今天已经赞过，或者我们还不是好友...",
]
THANKS_TEXTS = [
    "谢谢你的 {count} 个赞! 收下这些金币，拿去买点喝的吧",
    "收到了 {count} 个赞...感觉身体里涌出了力量！",
]


@dataclass(slots=True)
class LikeResult:
    count: int = 0
    supported: bool = True
    friend: bool | None = None
    """好友判定结果；None = 无法判定（协议端不支持或查询失败）"""


@dataclass(slots=True)
class _PendingThanks:
    nickname: str
    count: int = 0
    task: asyncio.Task | None = field(default=None, compare=False)


class LikeService:
    def __init__(
        self,
        social: SocialService | None = None,
        archive: MessageArchive | None = None,
        thanks_window: float = 30.0,
    ):
        self.social = social
        self.archive = archive
        self.thanks_window = thanks_window
        self._pending: dict[str, _PendingThanks] = {}

    # ---- 给用户点赞（「赞我」指令 / send_like 工具） ----------------------

    async def give(
        self, channel: Channel, user_id: str, times: int | None = None
    ) -> LikeResult:
        """点赞：先判好友，再逐批点到目标数量或每日限额。

        times: 目标赞数（1~50），None 表示尽量多。
        """
        friend = await channel.is_friend(user_id)
        if friend is False:
            # 「刚加上好友就来要赞」是最常见的场景，缓存里多半还没有他；
            # 落地成拒绝之前强制重查一次（真不是好友时也只多一次 API 调用）
            friend = await channel.is_friend(user_id, refresh=True)
            if friend is False:
                logger.info(f"点赞被拒：{user_id} 不在好友列表")
                return LikeResult(friend=False)
        cap = MAX_BATCHES * LIKE_BATCH
        target = cap if times is None else max(1, min(times, cap))
        total = 0
        while total < target:
            batch = min(LIKE_BATCH, target - total)
            try:
                ok = await channel.send_like(user_id, batch)
            except NotImplementedError:
                return LikeResult(supported=False, friend=friend)
            if not ok:
                break
            total += batch
        return LikeResult(count=total, friend=friend)

    def given_reply(self, result: LikeResult, bot_name: str) -> str:
        if not result.supported:
            return "这个聊天里点不了赞哦，去 QQ 上找我吧"
        if result.friend is False:
            return random.choice(NOT_FRIEND_TEXTS).format(bot=bot_name)
        if result.count > 0:
            return random.choice(GIVEN_TEXTS).format(
                count=result.count, bot=bot_name
            )
        if result.friend:
            return random.choice(ALREADY_TEXTS)
        return random.choice(UNKNOWN_FAIL_TEXTS)

    # ---- 用户给 bot 回赞：聚合 → 致谢 + 奖励 ------------------------------

    async def on_profile_like(
        self, channel: Channel, user_id: str, nickname: str, times: int
    ) -> None:
        entry = self._pending.get(user_id)
        if entry is None:
            entry = _PendingThanks(nickname=nickname)
            self._pending[user_id] = entry
            entry.task = asyncio.create_task(
                self._flush_after_window(channel, user_id)
            )
        entry.nickname = nickname or entry.nickname
        entry.count += max(1, times)
        if entry.count >= THANKS_THRESHOLD:
            if entry.task is not None:
                entry.task.cancel()
            await self._flush(channel, user_id)

    async def _flush_after_window(self, channel: Channel, user_id: str) -> None:
        try:
            await asyncio.sleep(self.thanks_window)
            await self._flush(channel, user_id)
        except asyncio.CancelledError:
            pass

    async def _flush(self, channel: Channel, user_id: str) -> None:
        entry = self._pending.pop(user_id, None)
        if entry is None or entry.count <= 0:
            return
        count = entry.count
        text = random.choice(THANKS_TEXTS).format(count=count)
        if self.social is not None:
            gold = random.randint(1, 5 * count)
            impression = round(random.uniform(0.01 * count, 0.02 * count), 2)
            await self.social.add_coins(user_id, gold, "profile_like")
            await self.social.add_impression(user_id, impression, entry.nickname)
            text += f"\n获得{gold}金币，提升了{impression:.2f}好感度"
        session = await self._thanks_session(channel, user_id)
        try:
            await channel.send_text(session, f"{entry.nickname}，{text}")
        except Exception:
            logger.exception(f"回赞致谢发送失败: {user_id}")
        logger.info(f"回赞致谢: {entry.nickname}({user_id}) x{count}")

    async def _thanks_session(self, channel: Channel, user_id: str) -> SessionKey:
        """优先发到用户最近活跃的群，否则私聊。"""
        if self.archive is not None:
            try:
                if group_id := await self.archive.last_group_of(user_id):
                    return SessionKey(
                        channel=channel.name, chat_type="group", chat_id=group_id
                    )
            except Exception:
                logger.exception("查询用户最近活跃群失败")
        return SessionKey(
            channel=channel.name, chat_type="private", chat_id=user_id
        )


def build_like_commands(likes: LikeService, resolve_assistant) -> list:
    """「赞我/点赞/给我点赞」指令。"""
    from ..commands import Command, exact

    async def like_cmd(incoming, channel, rest: str) -> bool:
        assistant = await resolve_assistant(incoming.session)
        result = await likes.give(channel, incoming.user_id)
        await channel.send_text(
            incoming.session, likes.given_reply(result, assistant.nickname)
        )
        return True

    return [Command(exact("赞我", "点赞", "给我点赞"), like_cmd)]
