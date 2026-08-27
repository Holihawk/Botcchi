"""商店：头像挂件与消耗品。

商品目录在代码内注册（同 FEATURES 的思路），数据库只存背包（user_items）
与佩戴状态（social_users.pendant）。挂件的贴合位置由 PendantSpec 描述，
加新挂件 = assets/img/pendants/ 丢一张透明 PNG + GOODS 加一行。

金币工具调用费（ToolCharger）也放在这里：经济系统的消费口之一。
"""

import random
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select

from ..db.engine import Database
from ..db.models import CoinLogRow, GameEscrowRow, SocialUserRow, UserItemRow
from ..tools.registry import Tool, ToolContext, tool
from .service import SocialService


@dataclass(frozen=True, slots=True)
class PendantSpec:
    """挂件在头像上的贴合参数（基于 104px 头像框左上角的偏移）"""

    asset: str
    """assets/img/ 下的相对路径"""
    dx: float
    dy: float
    width: float
    rotate: float = 0.0
    """绕贴图中心的旋转角度（度）"""


@dataclass(frozen=True, slots=True)
class Goods:
    id: str
    name: str
    price: int
    desc: str
    kind: Literal["pendant", "consumable"]
    pendant: PendantSpec | None = None
    one_time: bool = False
    """每人限购一次（挂件默认如此，靠背包唯一约束兜底）"""
    first_buy_impression: tuple[float, float] | None = None
    """首次购买赠送的好感度随机区间"""


GOODS: dict[str, Goods] = {
    g.id: g
    for g in (
        Goods(
            id="hairpin_pink",
            name="神秘发夹-粉",
            price=9999,
            desc="小波奇的发卡，非常可爱的发卡~",
            kind="pendant",
            # 锚点沿用旧版签到卡：头像左上角、旋转 20°
            pendant=PendantSpec(
                asset="hairpin_pink.png", dx=-37, dy=-8, width=62, rotate=20
            ),
            one_time=True,
            first_buy_impression=(8.0, 10.0),
        ),
        Goods(
            id="double_card",
            name="好感度双倍加持卡",
            price=300,
            desc="下次签到好感度必定双倍，签到时自动生效",
            kind="consumable",
        ),
        Goods(
            id="game_ticket",
            name="游戏券",
            price=50,
            desc="小游戏门票：决斗 3 张、24点 1 张、blackjack 2 张",
            kind="consumable",
        ),
    )
}


def goods_by_name(name: str) -> Goods | None:
    for g in GOODS.values():
        if g.name == name:
            return g
    return None


# ---- 背包操作（session 级助手，供本服务与签到事务内复用） -----------------


async def item_count(s, user_id: str, item_id: str) -> int:
    row = await s.scalar(
        select(UserItemRow).where(
            UserItemRow.user_id == user_id, UserItemRow.item_id == item_id
        )
    )
    return row.count if row else 0


async def add_item(s, user_id: str, item_id: str, n: int = 1) -> None:
    row = await s.scalar(
        select(UserItemRow).where(
            UserItemRow.user_id == user_id, UserItemRow.item_id == item_id
        )
    )
    if row is None:
        s.add(UserItemRow(user_id=user_id, item_id=item_id, count=n))
    else:
        row.count += n


async def take_item(s, user_id: str, item_id: str, n: int = 1) -> bool:
    """扣减背包物品；数量不足返回 False"""
    row = await s.scalar(
        select(UserItemRow).where(
            UserItemRow.user_id == user_id, UserItemRow.item_id == item_id
        )
    )
    if row is None or row.count < n:
        return False
    row.count -= n
    return True


# ---- 游戏券托管（崩溃恢复） -----------------------------------------------


def escrow_add(s, game: str, group_id: str, user_id: str, tickets: int) -> None:
    """入座扣券的同一事务里记托管，正常终局用 escrow_clear 整组删除"""
    s.add(
        GameEscrowRow(
            game=game, group_id=group_id, user_id=user_id, tickets=tickets
        )
    )


async def escrow_clear(s, game: str, group_id: str) -> None:
    from sqlalchemy import delete

    await s.execute(
        delete(GameEscrowRow).where(
            GameEscrowRow.game == game, GameEscrowRow.group_id == group_id
        )
    )


async def refund_escrows(db: Database) -> tuple[int, int]:
    """启动时退还全部残留托管（崩溃或重启丢掉的对局），返回 (人数, 券数)"""
    async with db.session() as s:
        rows = (await s.scalars(select(GameEscrowRow))).all()
        if not rows:
            return 0, 0
        users: set[str] = set()
        tickets = 0
        for r in rows:
            await add_item(s, r.user_id, "game_ticket", r.tickets)
            users.add(r.user_id)
            tickets += r.tickets
            await s.delete(r)
        await s.commit()
        return len(users), tickets


@dataclass(slots=True)
class ShelfEntry:
    """商店卡片/背包展示用：商品 + 当前用户的持有状态"""

    goods: Goods
    owned: int
    equipped: bool


class ShopService:
    def __init__(self, db: Database, social: SocialService):
        self.db = db
        self.social = social

    async def shelf(self, user_id: str) -> tuple[list[ShelfEntry], int]:
        """货架 + 用户余额。"""
        async with self.db.session() as s:
            user = await s.scalar(
                select(SocialUserRow).where(SocialUserRow.user_id == user_id)
            )
            pendant = user.pendant if user else None
            coins = user.coins if user else 0
            entries = [
                ShelfEntry(
                    goods=g,
                    owned=await item_count(s, user_id, g.id),
                    equipped=(g.id == pendant),
                )
                for g in GOODS.values()
            ]
            return entries, coins

    async def buy(
        self, user_id: str, user_name: str, name: str, count: int = 1
    ) -> str:
        goods = goods_by_name(name)
        if goods is None:
            names = "、".join(g.name for g in GOODS.values())
            return f"商店里没有「{name}」哦，现在有：{names}"
        if not 1 <= count <= 99:
            return "一次最多买 99 份哦"
        if goods.one_time:
            if count > 1:
                return f"「{goods.name}」每人限购一次，买一份就好啦~"
            count = 1
        async with self.db.session() as s:
            row = await self.social._get_or_create(s, user_id, user_name)
            if goods.one_time and await item_count(s, user_id, goods.id):
                return f"「{goods.name}」每人限购一次，你已经拥有啦~"
            total = goods.price * count
            if row.coins < total:
                need = f"{goods.price} 金币" if count == 1 else (
                    f"{goods.price}×{count} = {total} 金币"
                )
                return (
                    f"金币不够呢…「{goods.name}」要 {need}，"
                    f"你现在有 {row.coins} 金币"
                )
            row.coins -= total
            s.add(
                CoinLogRow(
                    user_id=user_id, amount=-total, reason=f"shop:{goods.id}"
                )
            )
            await add_item(s, user_id, goods.id, count)
            bought = goods.name if count == 1 else f"{goods.name}×{count}"
            lines = [f"购买「{bought}」成功！花费 {total} 金币"]
            if goods.kind == "pendant" and row.pendant is None:
                row.pendant = goods.id
                lines.append("已经自动帮你戴上啦，签到卡片上就能看到~")
            if goods.first_buy_impression is not None:
                lo, hi = goods.first_buy_impression
                added = round(random.uniform(lo, hi), 2)
                row.impression = round(row.impression + added, 2)
                lines.append(f"你和波奇的好感度 +{added:.2f}！")
            balance = row.coins
            await s.commit()
            lines.append(f"余额 {balance} 金币")
            return "\n".join(lines)

    async def equip(self, user_id: str, name: str) -> str:
        goods = goods_by_name(name)
        if goods is None or goods.kind != "pendant":
            return f"没有叫「{name}」的挂件哦"
        async with self.db.session() as s:
            if not await item_count(s, user_id, goods.id):
                return f"你还没有「{goods.name}」，先在商店购买吧"
            row = await self.social._get_or_create(s, user_id, "")
            row.pendant = goods.id
            await s.commit()
            return f"戴上「{goods.name}」啦，签到卡片上就能看到~"

    async def unequip(self, user_id: str) -> str:
        async with self.db.session() as s:
            row = await self.social._get_or_create(s, user_id, "")
            if row.pendant is None:
                return "你现在没有佩戴挂件哦"
            name = GOODS[row.pendant].name if row.pendant in GOODS else row.pendant
            row.pendant = None
            await s.commit()
            return f"取下「{name}」了，想它的时候再戴上吧~"

    async def inventory_text(self, user_id: str) -> str:
        entries, coins = await self.shelf(user_id)
        lines = [
            f"  {e.goods.name} ×{e.owned}"
            + ("（佩戴中）" if e.equipped else "")
            for e in entries
            if e.owned
        ]
        if not lines:
            return f"背包空空的…（金币 {coins}）发送「商店」看看有什么吧"
        return f"🎒 我的背包（金币 {coins}）\n" + "\n".join(lines)

    async def use_double_card(self, s, user_id: str) -> bool:
        """签到事务内消耗一张双倍卡；没有则返回 False。"""
        return await take_item(s, user_id, "double_card")


# ---- LLM 工具：佩戴/取下/背包（免费且非破坏性；购买只走显式指令） ----------


def build_shop_tools(shop: ShopService) -> list[Tool]:
    @tool(
        name="equip_pendant",
        description=(
            "为当前用户佩戴或取下头像挂件（佩戴后签到卡片头像会戴上）。"
            "name 为挂件名称（如「神秘发夹-粉」）；remove=true 表示取下当前挂件。"
            "只操作发起对话的用户自己，购买挂件请让用户发送「购买 商品名」指令。"
        ),
    )
    async def equip_pendant(
        ctx: ToolContext, name: str = "", remove: bool = False
    ) -> str:
        user_id = ctx.incoming.user_id
        if remove:
            return await shop.unequip(user_id)
        if not name:
            return "需要挂件名称，可先用 list_backpack 查看用户拥有的挂件"
        return await shop.equip(user_id, name)

    @tool(
        name="list_backpack",
        description=(
            "查看当前用户背包里的物品（挂件/消耗品）与金币余额。"
            "用户问自己有什么道具、有多少金币、戴着什么挂件时调用。"
        ),
    )
    async def list_backpack(ctx: ToolContext) -> str:
        return await shop.inventory_text(ctx.incoming.user_id)

    return [equip_pendant, list_backpack]


# ---- 金币工具调用费 --------------------------------------------------------


class ToolCharger:
    """LLM 工具按次收金币：costs 配置 {工具名: 金币}，超管免费。

    在工具执行前扣费（余额不足直接把提示返回给模型），执行抛异常时退款；
    工具内部自行捕获并返回失败文案的情况不退款（视为已消耗一次调用）。
    """

    def __init__(
        self,
        social: SocialService,
        costs: dict[str, int],
        is_superuser,
    ):
        self.social = social
        self.costs = {k: v for k, v in costs.items() if v > 0}
        self.is_superuser = is_superuser

    def cost_of(self, tool_name: str, user_id: str) -> int:
        if self.is_superuser(user_id):
            return 0
        return self.costs.get(tool_name, 0)

    async def take(self, tool_name: str, user_id: str) -> tuple[int, str | None]:
        """尝试扣费，返回 (实扣金额, 拒绝文案或 None)。"""
        cost = self.cost_of(tool_name, user_id)
        if cost <= 0:
            return 0, None
        if await self.social.spend_coins(user_id, cost, f"tool:{tool_name}"):
            return cost, None
        profile = await self.social.peek(user_id)
        balance = profile.coins if profile else 0
        return 0, (
            f"金币不足：使用该功能需要 {cost} 金币，"
            f"用户当前只有 {balance} 金币。请如实告知用户，签到可以获得金币。"
        )

    async def refund(self, tool_name: str, user_id: str, amount: int) -> None:
        if amount > 0:
            await self.social.add_coins(
                user_id, amount, f"tool_refund:{tool_name}"
            )


# ---- 内置指令：商店/购买/佩戴/背包 -----------------------------------------


def build_shop_commands(shop: ShopService | None, cards) -> list:
    """cards 为 SocialCards（货架卡片渲染），None 时纯文本货架"""
    from loguru import logger

    from ..commands import Command, exact, prefix
    from ..render import to_onebot_file

    _NO_DB = "未配置数据库，商店不可用"

    async def shelf_cmd(incoming, channel, rest: str) -> bool:
        if shop is None:
            await channel.send_text(incoming.session, _NO_DB)
            return True
        entries, coins = await shop.shelf(incoming.user_id)
        if cards is not None:
            try:
                png = await cards.shop_card(entries)
                await channel.send_image(incoming.session, to_onebot_file(png))
                return True
            except Exception:
                logger.exception("商店卡片渲染失败，退化为文本")
        lines = [
            f"  {e.goods.name} — {e.goods.price} 金币"
            + ("（佩戴中）" if e.equipped
               else f"（已拥有×{e.owned}）" if e.owned else "")
            for e in entries
        ]
        await channel.send_text(
            incoming.session,
            f"🛒 波奇的小卖部（金币 {coins}）\n" + "\n".join(lines)
            + "\n发送「购买 商品名」下单",
        )
        return True

    async def buy_cmd(incoming, channel, rest: str) -> bool:
        if shop is None:
            await channel.send_text(incoming.session, _NO_DB)
            return True
        if not rest:
            await channel.send_text(
                incoming.session, "要买什么呀？用法：购买 商品名 [数量]"
            )
            return True
        # 末尾的数字 token 视为购买数量：「购买 游戏券 5」
        count = 1
        name = rest
        head, _, tail = rest.rpartition(" ")
        if head and tail.isdigit():
            name, count = head.strip(), int(tail)
        reply = await shop.buy(
            incoming.user_id, incoming.user_name, name, count
        )
        await channel.send_text(incoming.session, reply)
        return True

    async def equip_cmd(incoming, channel, rest: str) -> bool:
        if shop is None:
            return False
        if not rest:
            await channel.send_text(
                incoming.session, "要戴哪个挂件？用法：佩戴 挂件名"
            )
            return True
        await channel.send_text(
            incoming.session, await shop.equip(incoming.user_id, rest)
        )
        return True

    async def unequip_cmd(incoming, channel, rest: str) -> bool:
        if shop is None:
            return False
        await channel.send_text(
            incoming.session, await shop.unequip(incoming.user_id)
        )
        return True

    async def backpack_cmd(incoming, channel, rest: str) -> bool:
        if shop is None:
            await channel.send_text(incoming.session, "未配置数据库，背包不可用")
            return True
        await channel.send_text(
            incoming.session, await shop.inventory_text(incoming.user_id)
        )
        return True

    return [
        Command(exact("商店"), shelf_cmd),
        Command(prefix("购买"), buy_cmd),
        Command(prefix("佩戴", "装备"), equip_cmd),
        Command(exact("取下挂件"), unequip_cmd),
        Command(exact("我的背包", "背包"), backpack_cmd),
    ]
