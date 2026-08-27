"""权限体系：数值等级 + 封禁 + 群功能开关/休眠。

判定唯一入口是 PermissionGate，身份取自消息上下文（不可被 LLM 伪造）。
"""

from .commands import PermCommands
from .features import FEATURES, Feature, feature_for_command, tools_of
from .gate import SUPERUSER_LEVEL, PermissionGate
from .store import Ban, GroupState, LevelGrant, PermStore

__all__ = [
    "FEATURES",
    "SUPERUSER_LEVEL",
    "Ban",
    "Feature",
    "GroupState",
    "LevelGrant",
    "PermCommands",
    "PermStore",
    "PermissionGate",
    "feature_for_command",
    "tools_of",
]
