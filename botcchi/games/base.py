"""游戏券小游戏的公共底座：券的收退、崩溃托管、对局定时器。

子类（决斗/24点/blackjack）声明 NAME / ESCROW_KIND / TICKET_COST，
对局对象只需满足鸭子协议：有 group_id 属性、timer 属性与 cancel_timer()。
"""

import asyncio
import random
from typing import Any

from loguru import logger

from ..channels.base import Channel
from ..db.engine import Database
from ..social.service import SocialService
from ..social.shop import add_item, escrow_add, escrow_clear, take_item

TICKET_ID = "game_ticket"


class TicketGameService:
    NAME = "小游戏"
    """日志与提示里的游戏名"""
    ESCROW_KIND = ""
    """game_escrows.game 的取值（崩溃退款按此归组）"""
    TICKET_COST = 1
    """入场消耗的游戏券张数"""

    def __init__(
        self,
        db: Database,
        social: SocialService,
        rng: random.Random | None = None,
    ):
        self.db = db
        self.social = social
        self.rng = rng or random.Random()
        self.games: dict[str, Any] = {}
        """group_id → 对局状态（内存态，崩溃由托管退款兜底）"""
        self._channel: Channel | None = None
        """最近一次交互用的 channel，供超时回调向群里发消息"""

    async def aclose(self) -> None:
        for game in self.games.values():
            game.cancel_timer()
        self.games.clear()

    # ---- 游戏券与托管 ----------------------------------------------------

    async def _take_tickets(self, group_id: str, user_id: str) -> bool:
        """入座扣券并记托管；券不足返回 False"""
        async with self.db.session() as s:
            ok = await take_item(s, user_id, TICKET_ID, self.TICKET_COST)
            if ok:
                escrow_add(
                    s, self.ESCROW_KIND, group_id, user_id, self.TICKET_COST
                )
                await s.commit()
            return ok

    async def _refund(self, *user_ids: str) -> None:
        async with self.db.session() as s:
            for uid in user_ids:
                await add_item(s, uid, TICKET_ID, self.TICKET_COST)
            await s.commit()

    async def _close_game(self, game: Any) -> None:
        """对局终结：出内存表并清游戏券托管（此后崩溃不再触发退款）"""
        self.games.pop(game.group_id, None)
        async with self.db.session() as s:
            await escrow_clear(s, self.ESCROW_KIND, game.group_id)
            await s.commit()

    # ---- 定时器 ----------------------------------------------------------

    def _schedule(self, game: Any, seconds: float, cb) -> None:
        """seconds 秒后触发 cb(game)；0 表示不调度（测试用）"""
        game.cancel_timer()
        if seconds <= 0:
            return

        async def run():
            try:
                await asyncio.sleep(seconds)
                await cb(game)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(f"{self.NAME}定时任务失败")

        game.timer = asyncio.create_task(run())
