"""游戏券小游戏的公共底座：券的收退、崩溃托管、对局定时器。

子类（决斗/24点/blackjack）声明 NAME / ESCROW_KIND / TICKET_COST，
对局对象只需满足鸭子协议：有 group_id 属性、timer 属性与 cancel_timer()。
"""

import asyncio
import random
from typing import Any

from loguru import logger
from sqlalchemy import select

from ..channels.base import Channel
from ..db.engine import Database
from ..db.models import CoinLogRow, GameEscrowRow
from ..social.service import SocialService
from ..social.shop import (
    GOODS,
    add_item,
    escrow_add,
    escrow_clear,
    item_count,
    take_item,
)

TICKET_ID = "game_ticket"
TICKET_PRICE = GOODS[TICKET_ID].price


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
        """入座优先扣券，余下入场费用金币支付，并记入同一笔托管。"""
        async with self.db.session() as s:
            user = await self.social._get_or_create(s, user_id, "")
            owned = await item_count(s, user_id, TICKET_ID)
            tickets = min(owned, self.TICKET_COST)
            coins = (self.TICKET_COST - tickets) * TICKET_PRICE
            if user.coins < coins:
                return False
            if tickets and not await take_item(s, user_id, TICKET_ID, tickets):
                return False
            if coins:
                user.coins -= coins
                s.add(
                    CoinLogRow(
                        user_id=user_id,
                        amount=-coins,
                        reason=f"game:{self.ESCROW_KIND}",
                    )
                )
            escrow_add(
                s,
                self.ESCROW_KIND,
                group_id,
                user_id,
                tickets,
                coins,
            )
            await s.commit()
            return True

    async def _refund(self, group_id: str, *user_ids: str) -> None:
        """退还指定玩家的原始入场费；不传玩家则退还该桌全部入场费。"""
        async with self.db.session() as s:
            stmt = select(GameEscrowRow).where(
                GameEscrowRow.game == self.ESCROW_KIND,
                GameEscrowRow.group_id == group_id,
            )
            if user_ids:
                stmt = stmt.where(GameEscrowRow.user_id.in_(user_ids))
            rows = list((await s.scalars(stmt)).all())
            for row in rows:
                await add_item(s, row.user_id, TICKET_ID, row.tickets)
                if row.coins:
                    user = await self.social._get_or_create(s, row.user_id, "")
                    user.coins += row.coins
                    s.add(
                        CoinLogRow(
                            user_id=row.user_id,
                            amount=row.coins,
                            reason=f"game_refund:{self.ESCROW_KIND}",
                        )
                    )
                await s.delete(row)
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
