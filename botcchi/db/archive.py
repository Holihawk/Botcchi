"""群消息归档的存取"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from .engine import Database
from .models import GroupMessageRow


@dataclass(slots=True)
class ArchivedMessage:
    user_id: str
    user_name: str
    text: str
    created_at: datetime


class MessageArchive:
    def __init__(self, db: Database):
        self.db = db

    async def add(
        self, group_id: str, user_id: str, user_name: str, text: str
    ) -> None:
        async with self.db.session() as s:
            s.add(
                GroupMessageRow(
                    group_id=group_id,
                    user_id=user_id,
                    user_name=user_name,
                    text=text[:2000],
                )
            )
            await s.commit()

    async def recent(self, group_id: str, limit: int) -> list[ArchivedMessage]:
        """最近 limit 条，按时间正序返回。"""
        async with self.db.session() as s:
            rows = await s.scalars(
                select(GroupMessageRow)
                .where(GroupMessageRow.group_id == group_id)
                .order_by(GroupMessageRow.id.desc())
                .limit(limit)
            )
            messages = [
                ArchivedMessage(
                    user_id=r.user_id,
                    user_name=r.user_name,
                    text=r.text,
                    created_at=r.created_at,
                )
                for r in rows.all()
            ]
        messages.reverse()
        return messages

    async def last_group_of(self, user_id: str) -> str | None:
        """用户最近一次发言所在的群（回赞致谢的投递目标）"""
        async with self.db.session() as s:
            return await s.scalar(
                select(GroupMessageRow.group_id)
                .where(GroupMessageRow.user_id == user_id)
                .order_by(GroupMessageRow.id.desc())
                .limit(1)
            )

    async def count(self, group_id: str) -> int:
        async with self.db.session() as s:
            return (
                await s.scalar(
                    select(func.count(GroupMessageRow.id)).where(
                        GroupMessageRow.group_id == group_id
                    )
                )
                or 0
            )


