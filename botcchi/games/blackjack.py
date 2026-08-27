"""Blackjack 21 点小游戏（纯文字，游戏券入场）。

- 「blackjack」开桌/入座（最多 8 人），发起者「开始」开局，120s 未开局
  流局退券；每人 2 张游戏券。指令用英文名避免误拦「晚上21点集合」。
- **对庄制**（默认，单人可玩）：每个玩家独立对抗庄家小波奇，明牌无信息
  泄露，全员并行拿牌/停牌。庄家一明一暗，玩家全部停牌后开暗牌、补到
  17 点停（软 17 停）；玩家爆牌即负。赔付：胜 +200、Blackjack 胜 +250、
  平局退还 2 张游戏券。
- **暗牌赛制**（「blackjack 暗牌」，至少 2 人）：玩家互比点数抢彩池——
  手牌与抽到的牌全部**私聊**发放，群里只公告拿牌张数；全员停牌后开牌，
  未爆的最高点（Blackjack 优先）均分奖池 180×人数，全爆则奖池回收。
  私聊发不出去（非好友）时发牌阶段直接流局退券。
- 90s 无人动作自动全员停牌结算；两副牌（104 张），不会抽穿。
"""

import asyncio
import random
from dataclasses import dataclass, field

from loguru import logger

from ..channels.base import Channel, IncomingMessage, SessionKey
from ..db.engine import Database
from ..social.service import SocialService
from .base import TicketGameService

TICKET_COST = 2
WIN_PRIZE = 200
BJ_PRIZE = 250
HIDDEN_POOL_PER_PLAYER = 180
MAX_PLAYERS = 8
DEALER_STAND = 17

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


def score(cards: list[str]) -> int:
    """A 按 11 计，爆则逐张退为 1（软硬牌处理，沿用旧版算法）"""
    total = 0
    aces = 0
    for card in cards:
        if card in ("J", "Q", "K"):
            total += 10
        elif card == "A":
            aces += 1
            total += 11
        else:
            total += int(card)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_blackjack(cards: list[str]) -> bool:
    return len(cards) == 2 and score(cards) == 21


def fmt(cards: list[str]) -> str:
    return "[" + " ".join(cards) + "]"


@dataclass
class Player:
    user_id: str
    name: str
    cards: list[str] = field(default_factory=list)
    stood: bool = False

    @property
    def busted(self) -> bool:
        return score(self.cards) > 21


@dataclass
class Game:
    group_id: str
    session: SessionKey
    initiator: str
    phase: str
    """lobby / playing"""
    mode: str = "对庄"
    """对庄 / 暗牌"""
    players: dict[str, Player] = field(default_factory=dict)
    deck: list[str] = field(default_factory=list)
    dealer: list[str] = field(default_factory=list)
    timer: asyncio.Task | None = None

    def cancel_timer(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None


class BlackjackService(TicketGameService):
    NAME = "21 点"
    ESCROW_KIND = "blackjack"
    TICKET_COST = TICKET_COST  # 模块常量进类，供基类扣券/托管使用

    def __init__(
        self,
        db: Database,
        social: SocialService,
        *,
        lobby_seconds: float = 120,
        play_seconds: float = 90,
        rng: random.Random | None = None,
    ):
        super().__init__(db, social, rng=rng)
        self.lobby_seconds = lobby_seconds
        self.play_seconds = play_seconds

    def wants(self, incoming: IncomingMessage) -> bool:
        return (
            incoming.session.chat_id in self.games
            or incoming.text.strip().lower().startswith("blackjack")
        )


    # ---- 游戏券 ------------------------------------------------------------

    NEED_TICKETS = (
        f"上桌要 {TICKET_COST} 张游戏券哦，"
        f"先发送「购买 游戏券 {TICKET_COST}」"
    )




    # ---- 入口 --------------------------------------------------------------

    async def handle(
        self, incoming: IncomingMessage, channel: Channel, blocked: bool
    ) -> bool:
        self._channel = channel
        text = incoming.text.strip()
        game = self.games.get(incoming.session.chat_id)

        if text.lower().startswith("blackjack"):
            if blocked:
                await channel.send_text(
                    incoming.session, "「21点」在本群已被关闭"
                )
                return True
            mode = text[len("blackjack"):].strip() or "对庄"
            if mode not in ("对庄", "暗牌"):
                await channel.send_text(
                    incoming.session,
                    "赛制有「对庄」（默认）和「暗牌」两种：\n"
                    "  blackjack —— 各自对抗庄家小波奇，单人可玩\n"
                    "  blackjack 暗牌 —— 手牌私聊保密，互比点数抢彩池（≥2 人）",
                )
                return True
            return await self._entry(incoming, channel, game, mode)

        if game is None:
            return False
        if game.phase == "lobby" and text in ("开", "开始", "开始游戏"):
            if incoming.user_id != game.initiator:
                return False
            return await self._start(incoming, channel, game)
        if game.phase == "playing":
            if text in ("拿牌", "要牌"):
                return await self._hit(incoming, channel, game)
            if text in ("停牌", "不要"):
                return await self._stand(incoming, channel, game)
        return False

    async def _entry(
        self,
        incoming: IncomingMessage,
        channel: Channel,
        game: Game | None,
        mode: str,
    ) -> bool:
        session = incoming.session
        uid, name = incoming.user_id, incoming.user_name
        if game is None:
            if not await self._take_tickets(session.chat_id, uid):
                await channel.send_text(session, self.NEED_TICKETS)
                return True
            game = Game(
                group_id=session.chat_id,
                session=session,
                initiator=uid,
                phase="lobby",
                mode=mode,
                players={uid: Player(uid, name)},
            )
            self.games[session.chat_id] = game
            self._schedule(game, self.lobby_seconds, self._lobby_timeout)
            if mode == "暗牌":
                rules = (
                    "手牌私聊保密、互比点数，未爆最高点均分奖池 "
                    f"{HIDDEN_POOL_PER_PLAYER}×人数（至少 2 人）"
                )
            else:
                rules = (
                    "各自对抗庄家小波奇，一个人也能玩；"
                    f"胜 +{WIN_PRIZE}、Blackjack +{BJ_PRIZE}、平局退券"
                )
            await channel.send_text(
                session,
                f"{name} 开了一桌 21 点·{mode}（每人 {TICKET_COST} 张游戏券，"
                f"最多 {MAX_PLAYERS} 人）！{rules}。\n"
                "发送「blackjack」入座，发起者发送「开始」开局",
            )
            return True
        if game.phase != "lobby":
            await channel.send_text(session, "本群这桌 21 点已经开打啦，下一局再来吧")
            return True
        if uid in game.players:
            await channel.send_text(session, "你已经在桌上了，稍安勿躁~")
            return True
        if len(game.players) >= MAX_PLAYERS:
            await channel.send_text(session, f"这桌已经坐满 {MAX_PLAYERS} 人啦")
            return True
        if not await self._take_tickets(session.chat_id, uid):
            await channel.send_text(session, self.NEED_TICKETS)
            return True
        game.players[uid] = Player(uid, name)
        await channel.send_text(
            session,
            f"{name} 入座！当前 {len(game.players)} 人，发起者发送「开始」开局",
        )
        return True

    # ---- 对局 --------------------------------------------------------------

    def _new_deck(self) -> list[str]:
        deck = RANKS * 8  # 两副牌 × 4 花色
        self.rng.shuffle(deck)
        return deck

    @staticmethod
    def _private(game: Game, user_id: str) -> SessionKey:
        return SessionKey(
            channel=game.session.channel, chat_type="private", chat_id=user_id
        )

    async def _start(
        self, incoming: IncomingMessage, channel: Channel, game: Game
    ) -> bool:
        if game.mode == "暗牌" and len(game.players) < 2:
            await channel.send_text(
                incoming.session, "暗牌赛制至少要 2 个人哦"
            )
            return True
        game.cancel_timer()
        game.phase = "playing"
        game.deck = self._new_deck()
        if game.mode == "暗牌":
            return await self._start_hidden(channel, game)
        lines = ["发牌！"]
        for p in game.players.values():
            p.cards = [game.deck.pop(), game.deck.pop()]
            s = score(p.cards)
            note = ""
            if is_blackjack(p.cards):
                p.stood = True
                note = " Blackjack!"
            lines.append(f"{p.name}: {fmt(p.cards)} 点数:{s}{note}")
        game.dealer = [game.deck.pop(), game.deck.pop()]
        lines.append(f"庄家小波奇: [{game.dealer[0]} ?] 暗牌一张")
        lines.append("发送「拿牌」补牌、「停牌」停止，全员停牌后开庄结算")
        await channel.send_text(game.session, "\n".join(lines))
        if all(p.stood for p in game.players.values()):
            await self._settle(channel, game)
        else:
            self._schedule(game, self.play_seconds, self._play_timeout)
        return True

    async def _start_hidden(self, channel: Channel, game: Game) -> None | bool:
        """暗牌发牌：手牌私聊，任何一封发不出去则流局退券。"""
        for p in game.players.values():
            p.cards = [game.deck.pop(), game.deck.pop()]
            note = ""
            if is_blackjack(p.cards):
                p.stood = True
                note = " Blackjack！已自动停牌"
            try:
                await channel.send_text(
                    self._private(game, p.user_id),
                    f"（暗牌）你的手牌：{fmt(p.cards)} "
                    f"点数:{score(p.cards)}{note}",
                )
            except Exception:
                await self._close_game(game)
                await self._refund(*game.players)
                await channel.send_text(
                    game.session,
                    f"给 {p.name} 的私聊发不出去（需要和小波奇是好友），"
                    "本局流局，游戏券已全部退还",
                )
                return True
        pool = HIDDEN_POOL_PER_PLAYER * len(game.players)
        stood = [p.name for p in game.players.values() if p.stood]
        lines = [
            f"暗牌发牌完毕！手牌已私聊各位，奖池 {pool} 金币。\n"
            "发送「拿牌」补牌（新牌也走私聊）、「停牌」停止，"
            "全员停牌后开牌：未爆最高点均分奖池"
        ]
        lines += [f"{name} 已停牌" for name in stood]
        await channel.send_text(game.session, "\n".join(lines))
        if all(p.stood for p in game.players.values()):
            await self._settle(channel, game)
        else:
            self._schedule(game, self.play_seconds, self._play_timeout)
        return True

    async def _hit(
        self, incoming: IncomingMessage, channel: Channel, game: Game
    ) -> bool:
        p = game.players.get(incoming.user_id)
        if p is None or p.stood:
            return False
        card = game.deck.pop()
        p.cards.append(card)
        s = score(p.cards)
        if game.mode == "暗牌":
            note = ""
            if s == 21:
                p.stood = True
                note = "，已自动停牌"
            elif s > 21:
                p.stood = True
                note = "，爆掉了…已自动停牌"
            try:
                await channel.send_text(
                    self._private(game, p.user_id),
                    f"（暗牌）你抽到 {card}，现在 {fmt(p.cards)} "
                    f"点数:{s}{note}",
                )
            except Exception:
                await channel.send_text(
                    game.session,
                    f"{p.name} 的私聊发不出去了…牌已计入手牌，开牌见分晓吧",
                )
            await channel.send_text(
                game.session,
                f"{p.name} 拿了第 {len(p.cards)} 张牌"
                + ("，已停牌" if p.stood else ""),
            )
        else:
            msg = f"{p.name} 抽到 {card}，现在 {fmt(p.cards)} 点数:{s}"
            if s == 21:
                p.stood = True
                msg += "，自动停牌"
            elif s > 21:
                p.stood = True
                msg += "，爆掉了…"
            await channel.send_text(game.session, msg)
        await self._after_action(channel, game)
        return True

    async def _stand(
        self, incoming: IncomingMessage, channel: Channel, game: Game
    ) -> bool:
        p = game.players.get(incoming.user_id)
        if p is None or p.stood:
            return False
        p.stood = True
        if game.mode == "暗牌":
            await channel.send_text(game.session, f"{p.name} 停牌")
        else:
            await channel.send_text(
                game.session,
                f"{p.name} 停牌（{fmt(p.cards)} 点数:{score(p.cards)}）",
            )
        await self._after_action(channel, game)
        return True

    async def _after_action(self, channel: Channel, game: Game) -> None:
        if all(p.stood for p in game.players.values()):
            await self._settle(channel, game)
        else:
            self._schedule(game, self.play_seconds, self._play_timeout)

    async def _settle(
        self, channel: Channel, game: Game, timed_out: bool = False
    ) -> None:
        game.cancel_timer()
        await self._close_game(game)
        if game.mode == "暗牌":
            await self._showdown(channel, game, timed_out)
            return
        # 庄家开暗牌，补到 17（软 17 停）
        while score(game.dealer) < DEALER_STAND:
            game.dealer.append(game.deck.pop())
        d_score = score(game.dealer)
        d_bj = is_blackjack(game.dealer)
        d_bust = d_score > 21
        d_note = " Blackjack!" if d_bj else (" 爆掉了…" if d_bust else "")

        lines = ["21 点结算" + ("（超时自动停牌）" if timed_out else "")]
        lines.append(f"庄家小波奇: {fmt(game.dealer)} 点数:{d_score}{d_note}")
        refund_ids: list[str] = []
        for p in game.players.values():
            s = score(p.cards)
            p_bj = is_blackjack(p.cards)
            if p.busted:
                outcome, mark = "爆牌", "💥"
            elif p_bj and not d_bj:
                outcome, mark = f"Blackjack 胜！+{BJ_PRIZE} 金币", "🎉"
                await self.social.add_coins(p.user_id, BJ_PRIZE, "blackjack")
            elif d_bust or s > d_score:
                outcome, mark = f"胜！+{WIN_PRIZE} 金币", "🏆"
                await self.social.add_coins(p.user_id, WIN_PRIZE, "blackjack")
            elif s == d_score and p_bj == d_bj:
                outcome, mark = f"平局，退还 {TICKET_COST} 张游戏券", "🤝"
                refund_ids.append(p.user_id)
            else:
                outcome, mark = "负", "💸"
            lines.append(f"{mark} {p.name}: {fmt(p.cards)} 点数:{s} — {outcome}")
        if refund_ids:
            await self._refund(*refund_ids)
        await channel.send_text(game.session, "\n".join(lines))

    async def _showdown(
        self, channel: Channel, game: Game, timed_out: bool
    ) -> None:
        """暗牌开牌：未爆最高点（Blackjack 优先）均分奖池。"""
        pool = HIDDEN_POOL_PER_PLAYER * len(game.players)
        valid = [p for p in game.players.values() if not p.busted]
        bj_players = [p for p in valid if is_blackjack(p.cards)]
        if bj_players:
            winners = bj_players
        elif valid:
            top = max(score(p.cards) for p in valid)
            winners = [p for p in valid if score(p.cards) == top]
        else:
            winners = []

        lines = ["21 点·暗牌 开牌！" + ("（超时自动停牌）" if timed_out else "")]
        for p in game.players.values():
            s = score(p.cards)
            if p.busted:
                status = " 爆掉了…"
            elif is_blackjack(p.cards):
                status = " Blackjack!"
            else:
                status = ""
            lines.append(f"{p.name}: {fmt(p.cards)} 点数:{s}{status}")
        if not winners:
            lines.append("无人生还…奖池回收")
        else:
            share = pool // len(winners)
            for p in winners:
                await self.social.add_coins(p.user_id, share, "blackjack")
                lines.append(f"🏆 {p.name} 赢得 {share} 金币")
        await channel.send_text(game.session, "\n".join(lines))

    # ---- 超时 --------------------------------------------------------------


    async def _lobby_timeout(self, game: Game) -> None:
        await self._close_game(game)
        await self._refund(*game.players)
        if self._channel is not None:
            try:
                await self._channel.send_text(
                    game.session, "21 点超时未开局，已流局，游戏券全部退还"
                )
            except Exception:
                logger.warning("21 点流局通知发送失败")

    async def _play_timeout(self, game: Game) -> None:
        for p in game.players.values():
            p.stood = True
        if self._channel is not None:
            await self._settle(self._channel, game, timed_out=True)
