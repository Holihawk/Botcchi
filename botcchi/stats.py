"""功能调用统计：WebUI 统计面板的数据源。

按条记录三类事件：command（指令按功能开关名归组）、tool（LLM 工具按
工具名）、chat（AI 对话回复）。记录失败只打日志，绝不影响业务链路。
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import func, select

from .db.engine import Database
from .db.models import UsageStatRow

if TYPE_CHECKING:
    from .channels.base import IncomingMessage

TZ = ZoneInfo("Asia/Shanghai")


class StatsService:
    def __init__(self, db: Database):
        self.db = db

    async def record(
        self, kind: str, name: str, incoming: "IncomingMessage"
    ) -> None:
        session = incoming.session
        await self.add(
            kind,
            name,
            user_id=incoming.user_id,
            user_name=incoming.user_name,
            group_id=session.chat_id if session.chat_type == "group" else "",
        )

    async def add(
        self,
        kind: str,
        name: str,
        *,
        user_id: str,
        user_name: str = "",
        group_id: str = "",
    ) -> None:
        try:
            async with self.db.session() as s:
                s.add(
                    UsageStatRow(
                        kind=kind,
                        name=name,
                        user_id=user_id,
                        user_name=user_name,
                        group_id=group_id,
                        day=datetime.now(TZ).date().isoformat(),
                    )
                )
                await s.commit()
        except Exception:
            logger.exception(f"调用统计写入失败: {kind}/{name}")

    async def overview(self, days: int = 14) -> dict[str, Any]:
        """窗口内的聚合：总量、按日、按功能/工具、活跃用户"""
        days = max(1, min(days, 90))
        today = datetime.now(TZ).date()
        since = (today - timedelta(days=days - 1)).isoformat()
        async with self.db.session() as s:
            total_all = await s.scalar(
                select(func.count()).select_from(UsageStatRow)
            )
            in_window = UsageStatRow.day >= since
            total = await s.scalar(
                select(func.count()).select_from(UsageStatRow).where(in_window)
            )
            daily_rows = (
                await s.execute(
                    select(UsageStatRow.day, func.count())
                    .where(in_window)
                    .group_by(UsageStatRow.day)
                )
            ).all()
            by_name_rows = (
                await s.execute(
                    select(
                        UsageStatRow.kind, UsageStatRow.name, func.count()
                    )
                    .where(in_window)
                    .group_by(UsageStatRow.kind, UsageStatRow.name)
                    .order_by(func.count().desc())
                )
            ).all()
            user_rows = (
                await s.execute(
                    select(
                        UsageStatRow.user_id,
                        func.max(UsageStatRow.user_name),
                        func.count(),
                    )
                    .where(in_window)
                    .group_by(UsageStatRow.user_id)
                    .order_by(func.count().desc())
                    .limit(10)
                )
            ).all()
            group_rows = (
                await s.execute(
                    select(UsageStatRow.group_id, func.count())
                    .where(in_window, UsageStatRow.group_id != "")
                    .group_by(UsageStatRow.group_id)
                    .order_by(func.count().desc())
                    .limit(10)
                )
            ).all()
        counts = dict(daily_rows)
        daily = [
            {"day": (d := (today - timedelta(days=i)).isoformat()),
             "count": counts.get(d, 0)}
            for i in reversed(range(days))
        ]
        return {
            "days": days,
            "total": total or 0,
            "total_all": total_all or 0,
            "today": counts.get(today.isoformat(), 0),
            "daily": daily,
            "by_name": [
                {"kind": k, "name": n, "count": c} for k, n, c in by_name_rows
            ],
            "top_users": [
                {"user_id": u, "user_name": n or "", "count": c}
                for u, n, c in user_rows
            ],
            "top_groups": [
                {"group_id": g, "count": c} for g, c in group_rows
            ],
        }
