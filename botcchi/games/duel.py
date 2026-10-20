"""轮盘决斗小游戏（恶魔轮盘赌改编，入场费由游戏券优先抵扣）。

- 1v1 约战：「@某人 决斗」/「决斗@某人」，45s 内「接受」开局；1 条命，
  道具池只有手铐/透镜；接受者先手。约 bot 触发旧版彩蛋。
- 多人局：「决斗」开大厅（再发「决斗」加入，最多 6 人），发起者「开始」开局；
  恰好 2 人时按 1v1 规则打；≥3 人每人 2 条命、全部四种道具；发起者先手，
  之后按加入顺序轮转。
- 弹夹 3n+1 发、实弹 n-1 或 n（n 为存活人数，开局公告确切实弹数）；
  打空后按存活人数重新装填并给每人补发一件道具。
- 空弹打自己继续行动；每人入场费为 3 张券（每张按 50 金币计），流局原路退还。
- 奖池 120×参赛人数：1v1 胜者全拿；多人 70%/20%/10% 前三名，
  超时判负者不参与分配（份额并入冠军）。
"""

import asyncio
import random
import re
from dataclasses import dataclass, field

from loguru import logger

from ..channels.base import Channel, IncomingMessage, SessionKey
from ..db.engine import Database
from ..social.service import SocialService
from .base import TICKET_PRICE, TicketGameService

TICKET_COST = 3
POOL_PER_PLAYER = 120

ITEMS = {
    "糖": "回复 1 点生命（上限 2）",
    "手铐": "铐住一名对手，跳过他的下一回合",
    "透镜": "偷看下一发是实弹还是空弹（私聊告知）",
    "延长管": "你打出的下一发若为实弹，伤害翻倍",
}
ITEMS_1V1 = ("手铐", "透镜")
ITEMS_MULTI = ("糖", "手铐", "透镜", "延长管")

CHICKEN_OUT = "那...那个...{name}不敢玩这么危险的游戏啦..."


@dataclass
class Player:
    user_id: str
    name: str
    hp: int = 1
    items: list[str] = field(default_factory=list)
    chosen: bool = False
    cuffed: bool = False
    barrel: bool = False
    """延长管待生效：下一发实弹伤害 ×2"""
    forfeited: bool = False


@dataclass
class Game:
    group_id: str
    session: SessionKey
    initiator: str
    phase: str
    """challenge / lobby / choosing / playing"""
    players: list[Player] = field(default_factory=list)
    """对局轮转顺序（choosing 前为加入顺序）"""
    challenge_target: str | None = None
    one_vs_one: bool = False
    magazine: list[bool] = field(default_factory=list)
    turn: int = 0
    eliminated: list[Player] = field(default_factory=list)
    timer: asyncio.Task | None = None

    def player(self, user_id: str) -> Player | None:
        for p in self.players:
            if p.user_id == user_id:
                return p
        return None

    @property
    def alive(self) -> list[Player]:
        return [p for p in self.players if p.hp > 0]

    @property
    def current(self) -> Player:
        return self.players[self.turn]

    def cancel_timer(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None


class DuelService(TicketGameService):
    NAME = "决斗"
    ESCROW_KIND = "duel"
    TICKET_COST = TICKET_COST  # 模块常量进类，供基类扣券/托管使用

    def __init__(
        self,
        db: Database,
        social: SocialService,
        *,
        challenge_seconds: float = 45,
        lobby_seconds: float = 120,
        choose_seconds: float = 30,
        turn_seconds: float = 60,
        rng: random.Random | None = None,
    ):
        super().__init__(db, social, rng=rng)
        self.challenge_seconds = challenge_seconds
        self.lobby_seconds = lobby_seconds
        self.choose_seconds = choose_seconds
        self.turn_seconds = turn_seconds

    def wants(self, incoming: IncomingMessage) -> bool:
        """快速预判，避免为每条群消息走权限查询"""
        return (
            incoming.session.chat_id in self.games
            or "决斗" in incoming.text[:24]
        )


    # ---- 游戏券 ------------------------------------------------------------




    NEED_TICKETS = (
        f"参加决斗需要 {TICKET_COST} 张游戏券或等值金币（游戏券优先），"
        f"可发送「购买 游戏券 {TICKET_COST}」"
    )

    # ---- 入口分发 ----------------------------------------------------------

    async def handle(
        self, incoming: IncomingMessage, channel: Channel, blocked: bool
    ) -> bool:
        self._channel = channel
        text = incoming.text.strip()
        game = self.games.get(incoming.session.chat_id)
        stripped = self._strip_mentions(text)

        if stripped == "决斗":
            if blocked:
                await channel.send_text(
                    incoming.session, "「决斗」在本群已被关闭"
                )
                return True
            if incoming.at_me and not incoming.mentions:
                # 彩蛋：约 bot 决斗
                await channel.send_text(
                    incoming.session,
                    CHICKEN_OUT.format(name="小波奇"),
                )
                return True
            return await self._entry(incoming, channel, game)

        if game is None:
            return False
        if game.phase == "challenge" and text in ("接受", "应战", "拒绝"):
            return await self._on_challenge_reply(incoming, channel, game, text)
        if game.phase == "lobby" and text in ("开", "开始", "开始游戏"):
            return await self._start(incoming, channel, game)
        if game.phase == "choosing" and (
            text.startswith("选") or len(self._match_items(game, text)) == 1
        ):
            # 「选/选择 透镜」「我选透镜」「透镜」都算数——只要道具名不含糊
            return await self._on_choose(incoming, channel, game, text)
        if game.phase == "playing":
            if text.startswith("用"):
                return await self._on_use_item(incoming, channel, game, text)
            if text.startswith("打"):
                return await self._on_shoot(incoming, channel, game, text)
        return False

    @staticmethod
    def _strip_mentions(text: str) -> str:
        return re.sub(r"@\S+", "", text).strip()

    async def _entry(
        self, incoming: IncomingMessage, channel: Channel, game: Game | None
    ) -> bool:
        session = incoming.session
        uid, name = incoming.user_id, incoming.user_name

        if incoming.mentions:
            # 1v1 约战
            target = incoming.mentions[0]
            if game is not None:
                await channel.send_text(session, "本群已经有一场决斗在进行啦")
                return True
            if target == uid:
                await channel.send_text(session, "不能和自己决斗哦")
                return True
            if not await self._take_tickets(session.chat_id, uid):
                await channel.send_text(session, self.NEED_TICKETS)
                return True
            game = Game(
                group_id=session.chat_id,
                session=session,
                initiator=uid,
                phase="challenge",
                challenge_target=target,
                one_vs_one=True,
                players=[Player(uid, name)],
            )
            self.games[session.chat_id] = game
            self._schedule(game, self.challenge_seconds, self._challenge_timeout)
            await channel.send_text(
                session,
                f"{name} 发起了决斗！{self.challenge_seconds:.0f} 秒内发送"
                "「接受」应战（「拒绝」婉拒）"
                f"入场费 {TICKET_COST} 张游戏券或 {TICKET_COST * TICKET_PRICE} 金币"
                "（有券优先），胜者全拿奖池！",
                at_user=target,
            )
            return True

        # 多人大厅：创建或加入
        if game is None:
            if not await self._take_tickets(session.chat_id, uid):
                await channel.send_text(session, self.NEED_TICKETS)
                return True
            game = Game(
                group_id=session.chat_id,
                session=session,
                initiator=uid,
                phase="lobby",
                players=[Player(uid, name)],
            )
            self.games[session.chat_id] = game
            self._schedule(game, self.lobby_seconds, self._lobby_timeout)
            await channel.send_text(
                session,
                f"{name} 开了一张决斗桌（每人 {TICKET_COST} 张游戏券或"
                f"{TICKET_COST * TICKET_PRICE} 金币，有券优先，最多 6 人）！"
                f"发送「决斗」加入，发起者发送「开始」开局，"
                f"{self.lobby_seconds:.0f} 秒未开局则流局并原路退还入场费",
            )
            return True
        if game.phase != "lobby":
            await channel.send_text(session, "本群的决斗已经开打啦，下一局再来吧")
            return True
        if game.player(uid) is not None:
            await channel.send_text(session, "你已经在桌上了，稍安勿躁~")
            return True
        if len(game.players) >= 6:
            await channel.send_text(session, "这桌已经坐满 6 人啦")
            return True
        if not await self._take_tickets(session.chat_id, uid):
            await channel.send_text(session, self.NEED_TICKETS)
            return True
        game.players.append(Player(uid, name))
        await channel.send_text(
            session,
            f"{name} 入座！当前 {len(game.players)} 人，"
            "发起者发送「开始」开局",
        )
        return True

    # ---- 约战应答 / 大厅开局 ----------------------------------------------

    async def _on_challenge_reply(
        self, incoming: IncomingMessage, channel: Channel, game: Game, text: str
    ) -> bool:
        if incoming.user_id != game.challenge_target:
            return False
        if text == "拒绝":
            game.cancel_timer()
            await self._refund(game.group_id, game.initiator)
            await self._close_game(game)
            await channel.send_text(
                incoming.session, "对方婉拒了决斗，入场费已原路退还"
            )
            return True
        if not await self._take_tickets(game.group_id, incoming.user_id):
            await channel.send_text(incoming.session, self.NEED_TICKETS)
            return True
        game.cancel_timer()
        game.players.append(Player(incoming.user_id, incoming.user_name))
        # 接受者先手
        game.players.reverse()
        await self._begin_choosing(channel, game)
        return True

    async def _start(
        self, incoming: IncomingMessage, channel: Channel, game: Game
    ) -> bool:
        if incoming.user_id != game.initiator:
            return False
        if len(game.players) < 2:
            await channel.send_text(
                incoming.session, "至少要 2 个人才能开局哦"
            )
            return True
        game.cancel_timer()
        if len(game.players) == 2:
            # 只有 2 人：按 1v1 约战规则打，后入座者先手
            game.one_vs_one = True
            game.players.reverse()
        await self._begin_choosing(channel, game)
        return True

    # ---- 选道具 ------------------------------------------------------------

    def _item_pool(self, game: Game) -> tuple[str, ...]:
        return ITEMS_1V1 if game.one_vs_one else ITEMS_MULTI

    def _match_items(self, game: Game, text: str) -> list[str]:
        """文本里出现的本局可选道具名（道具名之间互不为子串，直接包含判定）"""
        return [name for name in self._item_pool(game) if name in text]

    async def _begin_choosing(self, channel: Channel, game: Game) -> None:
        game.phase = "choosing"
        if not game.one_vs_one:
            for p in game.players:
                p.hp = 2
        pool = self._item_pool(game)
        menu = "\n".join(f"  {name}：{ITEMS[name]}" for name in pool)
        self._schedule(game, self.choose_seconds, self._choose_timeout)
        await channel.send_text(
            game.session,
            f"决斗开始！每人 {game.players[0].hp} 条命，奖池 "
            f"{POOL_PER_PLAYER * len(game.players)} 金币\n"
            f"开局前每人选一件道具（发送「选 道具名」或直接说道具名，"
            f"{self.choose_seconds:.0f} 秒不选随机发）：\n{menu}",
        )

    async def _on_choose(
        self, incoming: IncomingMessage, channel: Channel, game: Game, text: str
    ) -> bool:
        player = game.player(incoming.user_id)
        if player is None or player.chosen:
            return False
        matched = self._match_items(game, text)
        if not matched:
            await channel.send_text(
                incoming.session,
                "可选：" + " / ".join(self._item_pool(game)),
            )
            return True
        if len(matched) > 1:
            await channel.send_text(
                incoming.session, "一次只能选一个哦：" + " / ".join(matched)
            )
            return True
        name = matched[0]
        player.items.append(name)
        player.chosen = True
        if all(p.chosen for p in game.players):
            game.cancel_timer()
            await self._begin_playing(channel, game)
        return True

    # ---- 对局 --------------------------------------------------------------

    def _new_magazine(self, n_alive: int) -> list[bool]:
        shells = 3 * n_alive + 1
        live = self.rng.choice((n_alive - 1, n_alive)) or 1
        magazine = [True] * live + [False] * (shells - live)
        self.rng.shuffle(magazine)
        return magazine

    async def _begin_playing(self, channel: Channel, game: Game) -> None:
        game.phase = "playing"
        await self._reload(channel, game, first=True)
        await self._announce_turn(channel, game)

    async def _reload(
        self, channel: Channel, game: Game, first: bool = False
    ) -> None:
        alive = game.alive
        game.magazine = self._new_magazine(len(alive))
        live = sum(game.magazine)
        lines = [
            f"{'装填' if first else '换弹'}！{len(game.magazine)} 发弹药，"
            f"其中实弹 {live} 发"
        ]
        if not first:
            for p in alive:
                got = self.rng.choice(self._item_pool(game))
                p.items.append(got)
                lines.append(f"  {p.name} 获得道具：{got}")
        await channel.send_text(game.session, "\n".join(lines))

    def _shoot_hint(self, game: Game) -> str:
        return (
            "「打自己」或「打对面」"
            if game.one_vs_one
            else "「打自己」或「打@某人」"
        )

    async def _announce_turn(self, channel: Channel, game: Game) -> None:
        p = game.current
        held = "、".join(p.items) if p.items else "无"
        self._schedule(game, self.turn_seconds, self._turn_timeout)
        await channel.send_text(
            game.session,
            f"轮到你了！生命 {p.hp}，道具：{held}。"
            f"{self._shoot_hint(game)}开枪，「用 道具名」使用道具",
            at_user=p.user_id,
        )

    async def _on_use_item(
        self, incoming: IncomingMessage, channel: Channel, game: Game, text: str
    ) -> bool:
        p = game.current
        if incoming.user_id != p.user_id:
            return False
        rest = self._strip_mentions(text.removeprefix("用").strip())
        if rest not in ITEMS:
            return False
        if rest not in p.items:
            await channel.send_text(game.session, f"你手上没有「{rest}」哦")
            return True
        session = game.session
        if rest == "糖":
            if p.hp >= 2:
                await channel.send_text(session, "生命已满，糖先留着吧")
                return True
            p.hp += 1
            msg = f"{p.name} 吃了颗糖，生命回复到 {p.hp}"
        elif rest == "延长管":
            if p.barrel:
                await channel.send_text(session, "延长管已经装上了")
                return True
            p.barrel = True
            msg = f"{p.name} 装上了延长管，下一发实弹伤害翻倍！"
        elif rest == "透镜":
            peek = "实弹！" if game.magazine[0] else "空弹"
            private = SessionKey(
                channel=session.channel,
                chat_type="private",
                chat_id=p.user_id,
            )
            try:
                await channel.send_text(private, f"（悄悄话）下一发是：{peek}")
            except Exception:
                await channel.send_text(
                    session,
                    f"{p.name} 想偷看枪管，但悄悄话递不出去"
                    "（加个好友再用透镜吧），透镜还你",
                )
                return True
            msg = f"{p.name} 举起透镜看了一眼枪管…"
        else:  # 手铐
            if game.one_vs_one:
                target = next(
                    x for x in game.alive if x.user_id != p.user_id
                )
            else:
                tid = incoming.mentions[0] if incoming.mentions else ""
                target = game.player(tid)
                if target is None or target.hp <= 0 or target is p:
                    await channel.send_text(
                        session, "要铐谁？用法：用 手铐 @某人"
                    )
                    return True
            if target.cuffed:
                await channel.send_text(
                    session, f"{target.name} 已经被铐住了"
                )
                return True
            target.cuffed = True
            msg = f"{p.name} 铐住了 {target.name}，他的下一回合将被跳过！"
        p.items.remove(rest)
        await channel.send_text(session, msg)
        return True

    async def _on_shoot(
        self, incoming: IncomingMessage, channel: Channel, game: Game, text: str
    ) -> bool:
        p = game.current
        if incoming.user_id != p.user_id:
            return False
        stripped = self._strip_mentions(text)
        target: Player | None = None
        if stripped in ("打自己", "打我"):
            target = p
        elif game.one_vs_one and stripped in ("打对面", "打他", "打她"):
            target = next(x for x in game.alive if x.user_id != p.user_id)
        elif stripped == "打" and incoming.mentions:
            target = game.player(incoming.mentions[0])
            if target is None or target.hp <= 0:
                await channel.send_text(game.session, "他不在牌桌上")
                return True
        else:
            return False
        game.cancel_timer()
        await self._resolve_shot(channel, game, p, target)
        return True

    async def _resolve_shot(
        self, channel: Channel, game: Game, shooter: Player, target: Player
    ) -> None:
        session = game.session
        live = game.magazine.pop(0)
        boosted = shooter.barrel and live
        shooter.barrel = False
        at_self = shooter is target

        if not live:
            if at_self:
                await channel.send_text(
                    session,
                    f"咔——空弹！{shooter.name} 赌对了，继续行动！",
                )
                if game.magazine:
                    await self._announce_turn(channel, game)
                else:
                    await self._reload(channel, game)
                    await self._announce_turn(channel, game)
                return
            await channel.send_text(
                session, f"咔——空弹，{target.name} 捡回一条命"
            )
        else:
            damage = 2 if boosted else 1
            target.hp -= damage
            desc = "轰——实弹" + ("（延长管翻倍伤害）" if boosted else "")
            who = "自己" if at_self else target.name
            if target.hp <= 0:
                target.hp = 0
                game.eliminated.append(target)
                await channel.send_text(
                    session, f"{desc}！{shooter.name} 击中{who}，"
                    f"{target.name} 出局！"
                )
            else:
                await channel.send_text(
                    session,
                    f"{desc}！{shooter.name} 击中{who}，"
                    f"{target.name} 还剩 {target.hp} 条命",
                )

        if len(game.alive) <= 1:
            await self._finish(channel, game)
            return
        if not game.magazine:
            await self._reload(channel, game)
        self._advance_turn(game)
        await self._announce_turn(channel, game)

    def _advance_turn(self, game: Game) -> None:
        n = len(game.players)
        for _ in range(n * 2):
            game.turn = (game.turn + 1) % n
            p = game.current
            if p.hp <= 0:
                continue
            if p.cuffed:
                p.cuffed = False
                continue
            return

    # ---- 结算 --------------------------------------------------------------

    async def _finish(self, channel: Channel, game: Game) -> None:
        game.cancel_timer()
        await self._close_game(game)
        pool = POOL_PER_PLAYER * len(game.players)
        winner = game.alive[0]
        ranks = [winner, *reversed(game.eliminated)]
        if len(game.players) == 2:
            shares = [pool]
        else:
            # 70% / 20% / 10%，整数运算避免浮点截断
            shares = [pool * 7 // 10, pool * 2 // 10, pool // 10]
        payouts: list[tuple[Player, int]] = []
        champion_bonus = 0
        for i, share in enumerate(shares):
            if i >= len(ranks):
                break
            p = ranks[i]
            if p.forfeited:
                champion_bonus += share
            else:
                payouts.append((p, share))
        if champion_bonus and payouts:
            payouts[0] = (payouts[0][0], payouts[0][1] + champion_bonus)

        medals = ("🥇", "🥈", "🥉")
        lines = [f"决斗结束！{winner.name} 是最后的赢家！"]
        for i, (p, amount) in enumerate(payouts):
            await self.social.add_coins(p.user_id, amount, "duel")
            lines.append(f"{medals[i]} {p.name} +{amount} 金币")
        await channel.send_text(game.session, "\n".join(lines))

    # ---- 超时 --------------------------------------------------------------


    async def _challenge_timeout(self, game: Game) -> None:
        await self._refund(game.group_id, game.initiator)
        await self._close_game(game)
        await self._say(game, "没有等到应战，决斗流局，入场费已原路退还")

    async def _lobby_timeout(self, game: Game) -> None:
        await self._refund(game.group_id, *(p.user_id for p in game.players))
        await self._close_game(game)
        await self._say(game, "决斗桌超时未开局，已流局，入场费全部原路退还")

    async def _choose_timeout(self, game: Game) -> None:
        for p in game.players:
            if not p.chosen:
                p.items.append(self.rng.choice(self._item_pool(game)))
                p.chosen = True
        channel = self._channel
        if channel is not None:
            await self._begin_playing(channel, game)

    async def _turn_timeout(self, game: Game) -> None:
        p = game.current
        p.forfeited = True
        p.hp = 0
        game.eliminated.append(p)
        await self._say(game, f"{p.name} 发呆超时，被判出局！")
        channel = self._channel
        if channel is None:
            return
        if len(game.alive) <= 1:
            await self._finish(channel, game)
            return
        self._advance_turn(game)
        await self._announce_turn(channel, game)

    async def _say(self, game: Game, text: str) -> None:
        if self._channel is not None:
            try:
                await self._channel.send_text(game.session, text)
            except Exception:
                logger.warning("决斗超时通知发送失败")
