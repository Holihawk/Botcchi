"""Social 模块：签到、好感度、金币经济。

好感度按用户全局累计（不分群），等级注入 system prompt 影响 AI 态度；
金币走流水账本，为商店/银行/决斗等后续玩法留底。
"""

from .levels import FavorLevel, favor_level
from .service import SignResult, SocialProfile, SocialService

__all__ = [
    "FavorLevel",
    "SignResult",
    "SocialProfile",
    "SocialService",
    "favor_level",
]
