"""游戏券小游戏：决斗（轮盘赌）、24 点抢答、21 点（blackjack）。

公共底座（券的收退/崩溃托管/定时器）在 base.TicketGameService。
"""

from .base import TicketGameService
from .blackjack import BlackjackService
from .duel import DuelService
from .game24 import Game24Service

__all__ = [
    "BlackjackService",
    "DuelService",
    "Game24Service",
    "TicketGameService",
]
