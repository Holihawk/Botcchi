"""权限数据的持久层：等级授予、封禁、群设置"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, select

from ..db.engine import Database
from ..db.models import BanRow, GroupSettingRow, UserLevelRow


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite 读回的 datetime 无 tzinfo，统一补成 UTC"""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


@dataclass(frozen=True, slots=True)
class GroupState:
    active: bool = True
    blocked: frozenset[str] = frozenset()
    """群管关闭的功能"""
    su_blocked: frozenset[str] = frozenset()
    """超管关闭的功能（群管无法开启）"""

    @property
    def all_blocked(self) -> frozenset[str]:
        return self.blocked | self.su_blocked


@dataclass(frozen=True, slots=True)
class Ban:
    id: int
    user_id: str | None
    group_id: str | None
    until: datetime | None
    reason: str
    operator: str
    by_superuser: bool


@dataclass(frozen=True, slots=True)
class LevelGrant:
    id: int
    user_id: str
    group_id: str | None
    level: int
    granted_by: str = ""


DEFAULT_GROUP = GroupState()


@dataclass
class PermStore:
    db: Database
    _dirty_hooks: list = field(default_factory=list)

    def on_change(self, hook) -> None:
        """注册写操作后的回调（Gate 用于失效缓存）"""
        self._dirty_hooks.append(hook)

    def _notify(self) -> None:
        for hook in self._dirty_hooks:
            hook()

    # ---- 等级授予 -------------------------------------------------------

    async def grants_for(self, user_id: str) -> list[LevelGrant]:
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    select(UserLevelRow).where(UserLevelRow.user_id == user_id)
                )
            ).scalars()
            return [
                LevelGrant(r.id, r.user_id, r.group_id, r.level, r.granted_by)
                for r in rows
            ]

    async def get_grant(self, user_id: str, group_id: str | None) -> LevelGrant | None:
        async with self.db.session() as session:
            row = (
                await session.execute(
                    select(UserLevelRow).where(
                        UserLevelRow.user_id == user_id,
                        UserLevelRow.group_id == group_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return LevelGrant(row.id, row.user_id, row.group_id, row.level,
                              row.granted_by)

    async def set_level(
        self, user_id: str, group_id: str | None, level: int, granted_by: str = ""
    ) -> None:
        async with self.db.session() as session:
            row = (
                await session.execute(
                    select(UserLevelRow).where(
                        UserLevelRow.user_id == user_id,
                        UserLevelRow.group_id == group_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    UserLevelRow(
                        user_id=user_id, group_id=group_id,
                        level=level, granted_by=granted_by,
                    )
                )
            else:
                row.level = level
                row.granted_by = granted_by
            await session.commit()
        self._notify()

    async def remove_level(self, user_id: str, group_id: str | None) -> bool:
        async with self.db.session() as session:
            result = await session.execute(
                delete(UserLevelRow).where(
                    UserLevelRow.user_id == user_id,
                    UserLevelRow.group_id == group_id,
                )
            )
            await session.commit()
        self._notify()
        return bool(result.rowcount)

    async def remove_level_by_id(self, grant_id: int) -> bool:
        async with self.db.session() as session:
            result = await session.execute(
                delete(UserLevelRow).where(UserLevelRow.id == grant_id)
            )
            await session.commit()
        self._notify()
        return bool(result.rowcount)

    async def list_levels(self) -> list[LevelGrant]:
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    select(UserLevelRow).order_by(UserLevelRow.level.desc())
                )
            ).scalars()
            return [
                LevelGrant(r.id, r.user_id, r.group_id, r.level, r.granted_by)
                for r in rows
            ]

    # ---- 封禁 -----------------------------------------------------------

    async def find_ban(self, user_id: str, group_id: str | None) -> Ban | None:
        """查找对该用户在该场景生效的封禁（全局封 / 封整群 / 群内封）

        顺带清理已过期的记录
        """
        now = utcnow()
        async with self.db.session() as session:
            conditions = [
                (BanRow.user_id == user_id) & BanRow.group_id.is_(None),
            ]
            if group_id is not None:
                conditions.append(
                    BanRow.user_id.is_(None) & (BanRow.group_id == group_id)
                )
                conditions.append(
                    (BanRow.user_id == user_id) & (BanRow.group_id == group_id)
                )
            cond = conditions[0]
            for c in conditions[1:]:
                cond = cond | c
            rows = list(
                (await session.execute(select(BanRow).where(cond))).scalars()
            )
            expired = [r for r in rows if _aware(r.until) and _aware(r.until) < now]
            for r in expired:
                await session.delete(r)
            if expired:
                await session.commit()
                self._notify()
            for r in rows:
                if r in expired:
                    continue
                return Ban(r.id, r.user_id, r.group_id, _aware(r.until),
                           r.reason, r.operator, r.by_superuser)
            return None

    async def add_ban(
        self,
        user_id: str | None,
        group_id: str | None,
        until: datetime | None,
        reason: str,
        operator: str,
        by_superuser: bool,
    ) -> None:
        async with self.db.session() as session:
            # 同目标已有记录时覆盖（取更严格的操作者标记）
            existing = (
                await session.execute(
                    select(BanRow).where(
                        BanRow.user_id == user_id, BanRow.group_id == group_id
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.until = until
                existing.reason = reason
                existing.operator = operator
                existing.by_superuser = existing.by_superuser or by_superuser
            else:
                session.add(
                    BanRow(
                        user_id=user_id, group_id=group_id, until=until,
                        reason=reason, operator=operator, by_superuser=by_superuser,
                    )
                )
            await session.commit()
        self._notify()

    async def remove_ban(
        self, user_id: str | None, group_id: str | None, *, allow_su: bool
    ) -> bool | None:
        """解除封禁。返回 None 表示目标未被封禁；False 表示权限不足
        （超管下的封禁，群管无法解除）"""
        async with self.db.session() as session:
            row = (
                await session.execute(
                    select(BanRow).where(
                        BanRow.user_id == user_id, BanRow.group_id == group_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if row.by_superuser and not allow_su:
                return False
            await session.delete(row)
            await session.commit()
        self._notify()
        return True

    async def remove_ban_by_id(self, ban_id: int) -> bool:
        async with self.db.session() as session:
            result = await session.execute(delete(BanRow).where(BanRow.id == ban_id))
            await session.commit()
        self._notify()
        return bool(result.rowcount)

    async def list_bans(self) -> list[Ban]:
        async with self.db.session() as session:
            rows = (
                await session.execute(select(BanRow).order_by(BanRow.id.desc()))
            ).scalars()
            return [
                Ban(r.id, r.user_id, r.group_id, _aware(r.until),
                    r.reason, r.operator, r.by_superuser)
                for r in rows
            ]

    # ---- 群设置 ---------------------------------------------------------

    async def get_group(self, group_id: str) -> GroupState:
        async with self.db.session() as session:
            row = (
                await session.execute(
                    select(GroupSettingRow).where(
                        GroupSettingRow.group_id == group_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return DEFAULT_GROUP
            return GroupState(
                active=row.active,
                blocked=frozenset(row.blocked or []),
                su_blocked=frozenset(row.su_blocked or []),
            )

    async def update_group(
        self,
        group_id: str,
        *,
        active: bool | None = None,
        blocked: set[str] | None = None,
        su_blocked: set[str] | None = None,
    ) -> None:
        async with self.db.session() as session:
            row = (
                await session.execute(
                    select(GroupSettingRow).where(
                        GroupSettingRow.group_id == group_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = GroupSettingRow(group_id=group_id)
                session.add(row)
            if active is not None:
                row.active = active
            if blocked is not None:
                row.blocked = sorted(blocked)
            if su_blocked is not None:
                row.su_blocked = sorted(su_blocked)
            await session.commit()
        self._notify()

    async def list_groups(self) -> list[tuple[str, GroupState]]:
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    select(GroupSettingRow).order_by(GroupSettingRow.group_id)
                )
            ).scalars()
            return [
                (
                    r.group_id,
                    GroupState(
                        active=r.active,
                        blocked=frozenset(r.blocked or []),
                        su_blocked=frozenset(r.su_blocked or []),
                    ),
                )
                for r in rows
            ]
