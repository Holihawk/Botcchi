"""签到/好感度/金币的核心服务。

数值机制沿用旧版：好感度增量期望 1.5/(1+0.1*好感度) 的高斯采样（越高长得越慢，
单次 0.01~2.5），小概率双倍；金币 ~N(50,10) 截断到 [20,80]，另有连签加成。
日期按 Asia/Shanghai 归日。
"""

import asyncio
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from ..config import SocialSettings
from ..db.engine import Database
from ..db.models import CoinLogRow, SocialUserRow
from .levels import FavorLevel, favor_level

TZ = ZoneInfo("Asia/Shanghai")


def today() -> date:
    return datetime.now(TZ).date()


ALREADY_SIGNED_TEXTS = (
    "那、那个…你今天已经签过到啦，明天再来吧…",
    "唔…今天的份已经签过了哦，不、不用再签了…",
    "已、已经签过了啦！好感度…要明天才会再涨的…",
)


def already_signed_reply() -> str:
    """当天重复签到的提示文案（随机口癖）"""
    return random.choice(ALREADY_SIGNED_TEXTS)


@dataclass(slots=True)
class SocialProfile:
    user_id: str
    user_name: str
    impression: float
    coins: int
    sign_count: int
    streak: int
    last_sign_date: str | None
    rank: int = 0
    """好感度全局排名，0 表示未统计"""
    pendant: str | None = None
    """佩戴中的头像挂件商品 id"""

    @property
    def level(self) -> FavorLevel:
        return favor_level(self.impression)


@dataclass(slots=True)
class SignResult:
    signed: bool
    """False = 今天已经签过"""
    profile: SocialProfile
    impression_added: float = 0.0
    coins_added: int = 0
    streak_bonus: int = 0
    double: bool = False
    double_card: bool = False
    """本次双倍来自消耗「好感度双倍加持卡」"""


def _profile(row: SocialUserRow, rank: int = 0) -> SocialProfile:
    return SocialProfile(
        user_id=row.user_id,
        user_name=row.user_name,
        impression=round(row.impression, 2),
        coins=row.coins,
        sign_count=row.sign_count,
        streak=row.streak,
        last_sign_date=row.last_sign_date,
        rank=rank,
        pendant=row.pendant,
    )


class SocialService:
    def __init__(self, settings: SocialSettings, db: Database):
        self.settings = settings
        self.db = db
        self._sign_lock = asyncio.Lock()

    async def _get_or_create(self, s, user_id: str, user_name: str) -> SocialUserRow:
        row = await s.scalar(
            select(SocialUserRow).where(SocialUserRow.user_id == user_id)
        )
        if row is None:
            initial = self.settings.initial_coins
            row = SocialUserRow(
                user_id=user_id, user_name=user_name, coins=initial
            )
            s.add(row)
            if initial:
                s.add(CoinLogRow(user_id=user_id, amount=initial, reason="init"))
            await s.flush()
        elif user_name and row.user_name != user_name:
            row.user_name = user_name
        return row

    async def sign(self, user_id: str, user_name: str) -> SignResult:
        async with self._sign_lock, self.db.session() as s:
            row = await self._get_or_create(s, user_id, user_name)
            day = today()
            if row.last_sign_date == day.isoformat():
                rank = await self._rank_of(s, row.impression)
                await s.commit()
                return SignResult(signed=False, profile=_profile(row, rank))

            imp = row.impression
            expectation = 1.5 / (1 + imp * 0.1)
            added = random.gauss(expectation, expectation * 0.3)
            impression_added = round(max(0.01, min(added, 2.5)), 2)
            # 背包里有双倍卡则自动消耗一张，必定双倍；否则按概率
            from .shop import take_item

            double_card = await take_item(s, user_id, "double_card")
            double = double_card or (
                random.random() < self.settings.double_probability
            )
            if double:
                impression_added = round(impression_added * 2, 2)

            streak = (
                row.streak + 1
                if row.last_sign_date == (day - timedelta(days=1)).isoformat()
                else 1
            )
            base_coins = max(20, min(round(random.gauss(50, 10)), 80))
            streak_bonus = self.settings.streak_bonus_step * min(
                streak, self.settings.streak_bonus_cap
            )
            coins_added = base_coins + streak_bonus

            row.impression = round(imp + impression_added, 2)
            row.coins += coins_added
            row.sign_count += 1
            row.streak = streak
            row.last_sign_date = day.isoformat()
            s.add(CoinLogRow(user_id=user_id, amount=coins_added, reason="sign_in"))
            rank = await self._rank_of(s, row.impression)
            result = SignResult(
                signed=True,
                profile=_profile(row, rank),
                impression_added=impression_added,
                coins_added=coins_added,
                streak_bonus=streak_bonus,
                double=double,
                double_card=double_card,
            )
            await s.commit()
            return result

    async def _rank_of(self, s, impression: float) -> int:
        higher = await s.scalar(
            select(func.count())
            .select_from(SocialUserRow)
            .where(SocialUserRow.impression > impression)
        )
        return (higher or 0) + 1

    async def peek(self, user_id: str) -> SocialProfile | None:
        """只读查询，不为陌生用户建档"""
        async with self.db.session() as s:
            row = await s.scalar(
                select(SocialUserRow).where(SocialUserRow.user_id == user_id)
            )
            if row is None:
                return None
            return _profile(row, await self._rank_of(s, row.impression))

    async def profile(self, user_id: str, user_name: str = "") -> SocialProfile:
        async with self.db.session() as s:
            row = await self._get_or_create(s, user_id, user_name)
            rank = await self._rank_of(s, row.impression)
            await s.commit()
            return _profile(row, rank)

    async def top(self, limit: int = 10) -> list[SocialProfile]:
        async with self.db.session() as s:
            rows = await s.scalars(
                select(SocialUserRow)
                .order_by(SocialUserRow.impression.desc())
                .limit(limit)
            )
            return [_profile(r, i + 1) for i, r in enumerate(rows.all())]

    # ---- 金币账本（商店/银行/决斗等后续玩法的公共入口） -------------------

    async def add_coins(self, user_id: str, amount: int, reason: str) -> int:
        """入账并返回新余额"""
        async with self.db.session() as s:
            row = await self._get_or_create(s, user_id, "")
            row.coins += amount
            s.add(CoinLogRow(user_id=user_id, amount=amount, reason=reason))
            balance = row.coins
            await s.commit()
            return balance

    async def add_impression(
        self, user_id: str, amount: float, user_name: str = ""
    ) -> float:
        """加好感度（点赞回礼等非签到途径），返回新值"""
        async with self.db.session() as s:
            row = await self._get_or_create(s, user_id, user_name)
            row.impression = round(row.impression + amount, 2)
            value = row.impression
            await s.commit()
            return value

    async def set_coins(
        self, user_id: str, value: int, user_name: str = ""
    ) -> tuple[int, int]:
        """超管直接设置金币为指定值，返回 (变化量, 新余额)；差额记入流水"""
        async with self.db.session() as s:
            row = await self._get_or_create(s, user_id, user_name)
            delta = value - row.coins
            row.coins = value
            if delta:
                s.add(
                    CoinLogRow(user_id=user_id, amount=delta, reason="su_adjust")
                )
            await s.commit()
            return delta, value

    async def set_impression(
        self, user_id: str, value: float, user_name: str = ""
    ) -> float:
        """超管直接设置好感度为指定值，返回新值"""
        async with self.db.session() as s:
            row = await self._get_or_create(s, user_id, user_name)
            row.impression = round(value, 2)
            new = row.impression
            await s.commit()
            return new

    async def spend_coins(self, user_id: str, amount: int, reason: str) -> bool:
        """扣款；余额不足返回 False（不产生流水）"""
        async with self.db.session() as s:
            row = await self._get_or_create(s, user_id, "")
            if row.coins < amount:
                return False
            row.coins -= amount
            s.add(CoinLogRow(user_id=user_id, amount=-amount, reason=reason))
            await s.commit()
            return True

    # ---- 好感度 → system prompt 注入 --------------------------------------

    async def favor_block(self, user_id: str, user_name: str) -> str:
        """当前对话用户的好感度注入块；从未互动过的用户返回空串"""
        p = await self.peek(user_id)
        if p is None:
            return ""
        lv = p.level
        return (
            f"\n\n[好感度] 当前和你说话的用户 {user_name or p.user_name}"
            f"（好感度 {p.impression:.1f}，Lv.{lv.level} {lv.relation}）。"
            f"你对ta的态度：{lv.attitude}。"
        )
