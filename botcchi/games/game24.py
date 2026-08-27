"""24 点抢答小游戏（纯文字，游戏券入场）。

- 「24点」开桌/入座（最多 8 人，一群一桌），发起者「开始」开局，
  120s 未开局流局退券；每人 1 张游戏券，奖池 80×人数。
- 共 5 轮：每轮发 4 张牌（1-13，保证有解），玩家直接发算式抢答，
  最先答对者当场赢下本轮奖金（奖池均分到每轮）；每人每轮限 3 次提交。
- 无人解出（超时或全员用完次数）则公布一个解，该轮奖金顺延滚入下一轮；
  最后一轮仍无人解出则该份额回收。
- 路人（未入场者）喊出的正确算式会把该解法「作废」：玩家必须给出
  不同的解法（规范化比较，交换律/结合律变形视为同一个解）。
- 运算用分数精确计算（8/(3-8/3) 这类除法解不会被浮点误差误判）。
"""

import ast
import asyncio
import random
import re
from dataclasses import dataclass, field
from fractions import Fraction

from loguru import logger

from ..channels.base import Channel, IncomingMessage, SessionKey
from ..db.engine import Database
from ..social.service import SocialService
from .base import TicketGameService

TICKET_COST = 1
POOL_PER_PLAYER = 80
MAX_PLAYERS = 8
ATTEMPTS_PER_ROUND = 3
TARGET = Fraction(24)

_TRANS = str.maketrans(
    {"×": "*", "÷": "/", "（": "(", "）": ")", "＋": "+", "－": "-",
     "−": "-", "＊": "*", "／": "/", "　": " "}
)
_EXPR_RE = re.compile(r"[\d+\-*/() ]+")


def _normalize(text: str) -> str:
    return text.translate(_TRANS).strip()


def looks_like_expr(text: str) -> bool:
    t = _normalize(text)
    return bool(_EXPR_RE.fullmatch(t)) and any(op in t for op in "+-*/")


def parse_expr(text: str) -> tuple[Fraction, list[int], str] | None:
    """解析算式 → (值, 用到的数字, 规范化解法键)；非法返回 None。

    规范化键把加法/乘法链摊平排序，交换律与结合律的变形归为同一解。
    """
    try:
        tree = ast.parse(_normalize(text), mode="eval").body
    except SyntaxError:
        return None
    nums: list[int] = []

    def chain(op: str, lk: str, rk: str) -> str:
        parts: list[str] = []
        for k in (lk, rk):
            if k.startswith(f"({op}:"):
                parts.extend(k[3:-1].split("|"))
            else:
                parts.append(k)
        return f"({op}:" + "|".join(sorted(parts)) + ")"

    def walk(node: ast.expr) -> tuple[Fraction, str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            nums.append(node.value)
            return Fraction(node.value), str(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            v, k = walk(node.operand)
            return -v, f"(neg:{k})"
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, ast.Add | ast.Sub | ast.Mult | ast.Div
        ):
            lv, lk = walk(node.left)
            rv, rk = walk(node.right)
            if isinstance(node.op, ast.Add):
                return lv + rv, chain("+", lk, rk)
            if isinstance(node.op, ast.Mult):
                return lv * rv, chain("*", lk, rk)
            if isinstance(node.op, ast.Sub):
                return lv - rv, f"(-:{lk}|{rk})"
            if rv == 0:
                raise ZeroDivisionError
            return lv / rv, f"(/:{lk}|{rk})"
        raise ValueError(f"不支持的表达式节点: {type(node).__name__}")

    try:
        value, key = walk(tree)
    except (ValueError, ZeroDivisionError):
        return None
    return value, nums, key


def solve24(cards: list[int]) -> str | None:
    """返回一个凑出 24 的算式（美化显示用 ×÷），无解返回 None"""

    def rec(items: list[tuple[Fraction, str]]) -> str | None:
        if len(items) == 1:
            return items[0][1] if items[0][0] == TARGET else None
        for i in range(len(items)):
            for j in range(len(items)):
                if i == j:
                    continue
                (av, ae), (bv, be) = items[i], items[j]
                rest = [it for k, it in enumerate(items) if k not in (i, j)]
                cands = [
                    (av + bv, f"({ae}+{be})"),
                    (av * bv, f"({ae}×{be})"),
                    (av - bv, f"({ae}-{be})"),
                ]
                if bv != 0:
                    cands.append((av / bv, f"({ae}÷{be})"))
                for v, e in cands:
                    if (found := rec([*rest, (v, e)])) is not None:
                        return found
        return None

    result = rec([(Fraction(c), str(c)) for c in cards])
    return result[1:-1] if result else None  # 去掉最外层括号


@dataclass
class Game:
    group_id: str
    session: SessionKey
    initiator: str
    phase: str
    """lobby / playing"""
    players: dict[str, str] = field(default_factory=dict)
    """user_id → 昵称（入座顺序）"""
    rounds: int = 5
    round_no: int = 0
    cards: list[int] = field(default_factory=list)
    attempts: dict[str, int] = field(default_factory=dict)
    burned: set[str] = field(default_factory=set)
    """路人烧掉的解法（规范化键）"""
    per_round: int = 0
    carry: int = 0
    """无人解出顺延的奖金"""
    earnings: dict[str, int] = field(default_factory=dict)
    timer: asyncio.Task | None = None

    def cancel_timer(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None


class Game24Service(TicketGameService):
    NAME = "24 点"
    ESCROW_KIND = "game24"
    TICKET_COST = TICKET_COST  # 模块常量进类，供基类扣券/托管使用

    def __init__(
        self,
        db: Database,
        social: SocialService,
        *,
        rounds: int = 5,
        lobby_seconds: float = 120,
        round_seconds: float = 90,
        rng: random.Random | None = None,
    ):
        super().__init__(db, social, rng=rng)
        self.rounds = rounds
        self.lobby_seconds = lobby_seconds
        self.round_seconds = round_seconds

    def wants(self, incoming: IncomingMessage) -> bool:
        return (
            incoming.session.chat_id in self.games
            or incoming.text.strip().startswith("24点")
        )


    # ---- 游戏券 ------------------------------------------------------------

    NEED_TICKETS = (
        f"参加 24 点要 {TICKET_COST} 张游戏券哦，先发送「购买 游戏券」"
    )




    # ---- 入口 --------------------------------------------------------------

    async def handle(
        self, incoming: IncomingMessage, channel: Channel, blocked: bool
    ) -> bool:
        self._channel = channel
        text = incoming.text.strip()
        game = self.games.get(incoming.session.chat_id)

        if text == "24点":
            if blocked:
                await channel.send_text(
                    incoming.session, "「24点」在本群已被关闭"
                )
                return True
            return await self._entry(incoming, channel, game)

        if game is None:
            return False
        if game.phase == "lobby" and text in ("开", "开始", "开始游戏"):
            if incoming.user_id != game.initiator:
                return False
            return await self._start(incoming, channel, game)
        if game.phase == "playing" and looks_like_expr(text):
            return await self._on_answer(incoming, channel, game, text)
        return False

    async def _entry(
        self, incoming: IncomingMessage, channel: Channel, game: Game | None
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
                players={uid: name},
                rounds=self.rounds,
            )
            self.games[session.chat_id] = game
            self._schedule(game, self.lobby_seconds, self._lobby_timeout)
            await channel.send_text(
                session,
                f"{name} 开了一桌 24 点（每人 {TICKET_COST} 张游戏券，"
                f"最多 {MAX_PLAYERS} 人）！发送「24点」入座，"
                f"发起者发送「开始」开局，{self.lobby_seconds:.0f} 秒未开局流局退券",
            )
            return True
        if game.phase != "lobby":
            await channel.send_text(session, "本群的 24 点已经开打啦，下一局再来吧")
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
        game.players[uid] = name
        await channel.send_text(
            session,
            f"{name} 入座！当前 {len(game.players)} 人，发起者发送「开始」开局",
        )
        return True

    async def _start(
        self, incoming: IncomingMessage, channel: Channel, game: Game
    ) -> bool:
        if len(game.players) < 2:
            await channel.send_text(incoming.session, "至少要 2 个人才能开局哦")
            return True
        game.cancel_timer()
        game.phase = "playing"
        pool = POOL_PER_PLAYER * len(game.players)
        game.per_round = pool // game.rounds
        await channel.send_text(
            incoming.session,
            f"24 点开赛！共 {game.rounds} 轮，奖池 {pool} 金币按轮均分。\n"
            "规则：用 4 个数字和 + - × ÷ 括号凑出 24，直接发算式抢答，"
            f"每人每轮限 {ATTEMPTS_PER_ROUND} 次；无人解出奖金顺延下轮。\n"
            "注意：没入场的路人喊出的正确答案会让该解法作废！",
        )
        await self._deal(channel, game)
        return True

    # ---- 发牌与作答 --------------------------------------------------------

    def _new_cards(self) -> list[int]:
        while True:
            cards = [self.rng.randint(1, 13) for _ in range(4)]
            if solve24(cards) is not None:
                return cards

    async def _deal(self, channel: Channel, game: Game) -> None:
        game.round_no += 1
        game.cards = self._new_cards()
        game.attempts = {}
        game.burned = set()
        prize = game.per_round + game.carry
        bonus = "（含顺延奖金！）" if game.carry else ""
        self._schedule(game, self.round_seconds, self._round_timeout)
        await channel.send_text(
            game.session,
            f"第 {game.round_no}/{game.rounds} 轮，本轮奖金 {prize}{bonus}\n"
            f"牌面：{'  '.join(map(str, game.cards))}",
        )

    async def _on_answer(
        self, incoming: IncomingMessage, channel: Channel, game: Game, text: str
    ) -> bool:
        session = game.session
        uid, name = incoming.user_id, incoming.user_name
        parsed = parse_expr(text)
        is_player = uid in game.players

        if not is_player:
            # 路人：喊对了就烧掉该解法
            if parsed is None:
                return False
            value, nums, key = parsed
            if (
                value == TARGET
                and sorted(nums) == sorted(game.cards)
                and key not in game.burned
            ):
                game.burned.add(key)
                await channel.send_text(
                    session,
                    f"围观群众 {name} 喊出了正确答案！"
                    "这个解法作废，场上玩家要换一种解法哦",
                )
                return True
            return False

        used = game.attempts.get(uid, 0)
        if used >= ATTEMPTS_PER_ROUND:
            await channel.send_text(session, f"{name} 本轮 3 次机会已用完啦")
            return True
        if parsed is None:
            game.attempts[uid] = used + 1
            await channel.send_text(
                session,
                f"算式没看懂…（{name} 还剩 "
                f"{ATTEMPTS_PER_ROUND - used - 1} 次）",
            )
            await self._check_all_exhausted(channel, game)
            return True
        value, nums, key = parsed
        if sorted(nums) != sorted(game.cards):
            game.attempts[uid] = used + 1
            await channel.send_text(
                session,
                f"要用且只用牌面的 4 个数字哦（{name} 还剩 "
                f"{ATTEMPTS_PER_ROUND - used - 1} 次）",
            )
            await self._check_all_exhausted(channel, game)
            return True
        if value != TARGET:
            game.attempts[uid] = used + 1
            await channel.send_text(
                session,
                f"算出来是 {self._fmt(value)}，不是 24（{name} 还剩 "
                f"{ATTEMPTS_PER_ROUND - used - 1} 次）",
            )
            await self._check_all_exhausted(channel, game)
            return True
        if key in game.burned:
            # 与路人喊出的解法相同：不扣次数，换一个
            await channel.send_text(
                session,
                f"这个解法已经被路人喊出来作废啦，{name} 换一个"
                "不同的解法试试（不扣次数）",
            )
            return True

        # 答对！
        game.cancel_timer()
        prize = game.per_round + game.carry
        game.carry = 0
        game.earnings[uid] = game.earnings.get(uid, 0) + prize
        await self.social.add_coins(uid, prize, "game24")
        await channel.send_text(
            session, f"答对啦！{name} 抢下本轮，+{prize} 金币！"
        )
        await self._next_round(channel, game)
        return True

    @staticmethod
    def _fmt(value: Fraction) -> str:
        return str(value.numerator) if value.denominator == 1 else str(value)

    async def _check_all_exhausted(self, channel: Channel, game: Game) -> None:
        if all(
            game.attempts.get(uid, 0) >= ATTEMPTS_PER_ROUND
            for uid in game.players
        ):
            game.cancel_timer()
            await self._round_unsolved(channel, game, "大家的机会都用完了")

    async def _round_unsolved(
        self, channel: Channel, game: Game, why: str
    ) -> None:
        solution = solve24(game.cards) or "？"
        game.carry += game.per_round
        last = game.round_no >= game.rounds
        note = "该轮奖金回收" if last else "奖金顺延到下一轮"
        if last:
            game.carry = 0
        await channel.send_text(
            game.session,
            f"{why}，本轮无人解出！参考答案：{solution} = 24，{note}",
        )
        await self._next_round(channel, game)

    async def _next_round(self, channel: Channel, game: Game) -> None:
        if game.round_no >= game.rounds:
            await self._finish(channel, game)
            return
        await self._deal(channel, game)

    async def _finish(self, channel: Channel, game: Game) -> None:
        game.cancel_timer()
        await self._close_game(game)
        if not game.earnings:
            await channel.send_text(
                game.session, "全场颗粒无收…下次再来挑战吧！"
            )
            return
        board = sorted(game.earnings.items(), key=lambda kv: -kv[1])
        lines = ["24 点收官！本局战报："]
        medals = ("🥇", "🥈", "🥉")
        for i, (uid, amount) in enumerate(board):
            mark = medals[i] if i < len(medals) else "  "
            lines.append(f"{mark} {game.players.get(uid, uid)} +{amount} 金币")
        await channel.send_text(game.session, "\n".join(lines))

    # ---- 超时 --------------------------------------------------------------


    async def _lobby_timeout(self, game: Game) -> None:
        await self._close_game(game)
        await self._refund(*game.players)
        if self._channel is not None:
            try:
                await self._channel.send_text(
                    game.session, "24 点超时未开局，已流局，游戏券全部退还"
                )
            except Exception:
                logger.warning("24 点流局通知发送失败")

    async def _round_timeout(self, game: Game) -> None:
        if self._channel is not None:
            await self._round_unsolved(self._channel, game, "时间到")
