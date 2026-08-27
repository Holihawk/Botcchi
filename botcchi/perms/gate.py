"""PermissionGate：全部权限判定的唯一入口。

原则：判定只认 ToolContext / IncomingMessage 里的真实身份，不信 LLM 的任何参数。
热路径（每条群消息都会查封禁与群状态）带一层短 TTL 缓存，写操作即时失效。
"""

import time
from typing import TYPE_CHECKING, Any

from ..config import PermsSettings
from .features import tools_of
from .store import DEFAULT_GROUP, GroupState, PermStore

if TYPE_CHECKING:
    from ..channels.base import IncomingMessage

SUPERUSER_LEVEL = 999
CACHE_TTL = 15.0

_ROLE_KEYS = {"owner": "owner_level", "admin": "admin_level"}


class PermissionGate:
    def __init__(
        self,
        settings: PermsSettings,
        superusers: list[str],
        store: PermStore | None = None,
    ):
        """store 为 None（未配置数据库）时授予/封禁/群开关不可用，
        仅保留超管与群内身份的隐式等级"""
        self.settings = settings
        self.superusers = set(superusers)
        self.store = store
        self._cache: dict[Any, tuple[float, Any]] = {}
        if store is not None:
            store.on_change(self.invalidate)

    def invalidate(self) -> None:
        self._cache.clear()

    async def _cached(self, key: Any, load) -> Any:
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        value = await load()
        self._cache[key] = (now + CACHE_TTL, value)
        return value

    # ---- 等级 -----------------------------------------------------------

    def is_superuser(self, user_id: str) -> bool:
        return user_id in self.superusers

    def role_level(self, sender_role: str | None) -> int:
        """OneBot 群内身份（owner/admin）映射的隐式等级"""
        if sender_role in _ROLE_KEYS:
            return getattr(self.settings, _ROLE_KEYS[sender_role])
        return 0

    async def effective_level(
        self,
        user_id: str,
        group_id: str | None = None,
        sender_role: str | None = None,
    ) -> int:
        if self.is_superuser(user_id):
            return SUPERUSER_LEVEL
        level = self.role_level(sender_role)
        if self.store is not None:
            grants = await self._cached(
                ("grants", user_id), lambda: self.store.grants_for(user_id)
            )
            for g in grants:
                if g.group_id is None or g.group_id == group_id:
                    level = max(level, g.level)
        return level

    async def level_of(self, incoming: "IncomingMessage") -> int:
        group_id = (
            incoming.session.chat_id
            if incoming.session.chat_type == "group"
            else None
        )
        return await self.effective_level(
            incoming.user_id, group_id, incoming.sender_role
        )

    async def can_manage(self, incoming: "IncomingMessage") -> bool:
        """是否可使用群管理指令（定时设置/功能开关/封禁等）"""
        return await self.level_of(incoming) >= self.settings.manage_level

    # ---- 封禁 -----------------------------------------------------------

    async def is_banned(self, user_id: str, group_id: str | None = None) -> bool:
        if self.store is None or self.is_superuser(user_id):
            return False
        ban = await self._cached(
            ("ban", user_id, group_id),
            lambda: self.store.find_ban(user_id, group_id),
        )
        return ban is not None

    # ---- 群状态与功能开关 ------------------------------------------------

    async def group_state(self, group_id: str) -> GroupState:
        if self.store is None:
            return DEFAULT_GROUP
        return await self._cached(
            ("group", group_id), lambda: self.store.get_group(group_id)
        )

    async def feature_blocked(self, group_id: str, feature: str) -> bool:
        state = await self.group_state(group_id)
        return feature in state.all_blocked

    async def disabled_tools(self, group_id: str | None) -> frozenset[str]:
        """群内被禁用功能覆盖的工具名集合（私聊无功能开关，恒为空）"""
        if group_id is None:
            return frozenset()
        state = await self.group_state(group_id)
        return tools_of(state.all_blocked)
