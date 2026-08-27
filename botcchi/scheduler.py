"""统一定时任务调度器：DB 落库的 job + 单 tick 循环 + 按 kind 注册的 handler。

定时总结、孤独日报、提醒等功能各自注册一种 job kind 的执行器，
调度器只负责「到点了调谁」。时间按 Asia/Shanghai 理解，存储为 UTC；
到期判定在 Python 侧完成，规避 SQLite 无时区列的比较陷阱。

防重复语义沿用旧定时总结：执行前先推进/移除任务（宁可漏发不重复刷屏）。
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import delete, select

from .channels.base import Channel, SessionKey
from .db.engine import Database
from .db.models import ScheduledJobRow

TZ = ZoneInfo("Asia/Shanghai")


@dataclass(slots=True)
class ScheduledJob:
    id: int
    kind: str
    payload: dict[str, Any]
    session: SessionKey
    next_run_at: datetime
    recurrence: str | None
    created_by: str


Handler = Callable[[ScheduledJob, Channel], Awaitable[None]]


def _aware(dt: datetime) -> datetime:
    """SQLite 会丢失时区信息；无时区值一律按 UTC 理解"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def next_daily_run(hour: int, minute: int, after: datetime) -> datetime:
    """after 之后（严格晚于）的下一个本地 HH:MM，返回 UTC"""
    local = _aware(after).astimezone(TZ)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def parse_daily(recurrence: str) -> tuple[int, int] | None:
    """"daily@HH:MM" → (hour, minute)；不认识的格式返回 None"""
    if not recurrence.startswith("daily@"):
        return None
    try:
        hour_s, _, minute_s = recurrence.removeprefix("daily@").partition(":")
        hour, minute = int(hour_s), int(minute_s or 0)
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def _to_job(row: ScheduledJobRow) -> ScheduledJob:
    return ScheduledJob(
        id=row.id,
        kind=row.kind,
        payload=json.loads(row.payload or "{}"),
        session=SessionKey.parse(row.session_key),
        next_run_at=_aware(row.next_run_at),
        recurrence=row.recurrence,
        created_by=row.created_by,
    )


class JobStore:
    def __init__(self, db: Database):
        self.db = db

    async def add(
        self,
        kind: str,
        session: SessionKey,
        payload: dict[str, Any],
        next_run_at: datetime,
        recurrence: str | None = None,
        created_by: str = "",
    ) -> int:
        async with self.db.session() as s:
            row = ScheduledJobRow(
                kind=kind,
                payload=json.dumps(payload, ensure_ascii=False),
                session_key=str(session),
                next_run_at=next_run_at.astimezone(UTC),
                recurrence=recurrence,
                created_by=created_by,
            )
            s.add(row)
            await s.commit()
            return row.id

    async def due(self, now: datetime | None = None) -> list[ScheduledJob]:
        now = now or datetime.now(UTC)
        async with self.db.session() as s:
            rows = await s.scalars(
                select(ScheduledJobRow).where(ScheduledJobRow.enabled.is_(True))
            )
            return [
                _to_job(r) for r in rows.all() if _aware(r.next_run_at) <= now
            ]

    async def reschedule(self, job_id: int, next_run_at: datetime) -> None:
        async with self.db.session() as s:
            row = await s.get(ScheduledJobRow, job_id)
            if row is not None:
                row.next_run_at = next_run_at.astimezone(UTC)
                await s.commit()

    async def remove(self, job_id: int) -> bool:
        async with self.db.session() as s:
            result = await s.execute(
                delete(ScheduledJobRow).where(ScheduledJobRow.id == job_id)
            )
            await s.commit()
            return result.rowcount > 0

    async def remove_matching(
        self, kind: str, session_key: str, created_by: str | None = None
    ) -> int:
        stmt = delete(ScheduledJobRow).where(
            ScheduledJobRow.kind == kind,
            ScheduledJobRow.session_key == session_key,
        )
        if created_by is not None:
            stmt = stmt.where(ScheduledJobRow.created_by == created_by)
        async with self.db.session() as s:
            result = await s.execute(stmt)
            await s.commit()
            return result.rowcount

    async def list_jobs(
        self,
        kind: str | None = None,
        session_key: str | None = None,
        created_by: str | None = None,
    ) -> list[ScheduledJob]:
        stmt = select(ScheduledJobRow).where(ScheduledJobRow.enabled.is_(True))
        if kind is not None:
            stmt = stmt.where(ScheduledJobRow.kind == kind)
        if session_key is not None:
            stmt = stmt.where(ScheduledJobRow.session_key == session_key)
        if created_by is not None:
            stmt = stmt.where(ScheduledJobRow.created_by == created_by)
        stmt = stmt.order_by(ScheduledJobRow.next_run_at)
        async with self.db.session() as s:
            rows = await s.scalars(stmt)
            return [_to_job(r) for r in rows.all()]


class Scheduler:
    def __init__(self, store: JobStore, tick_seconds: float = 30.0):
        self.store = store
        self.tick_seconds = tick_seconds
        self._handlers: dict[str, Handler] = {}

    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    async def run(self, channel: Channel, stop: asyncio.Event) -> None:
        logger.info(f"定时任务调度器已启动（kinds={list(self._handlers)}）")
        while not stop.is_set():
            try:
                await self._tick(channel)
            except Exception:
                logger.exception("调度器 tick 失败")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.tick_seconds)
            except TimeoutError:
                pass
        logger.info("定时任务调度器已停止")

    async def _tick(self, channel: Channel, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        for job in await self.store.due(now):
            # 先推进/移除再执行：执行崩溃时宁可漏发，不重复刷屏
            if job.recurrence and (daily := parse_daily(job.recurrence)):
                await self.store.reschedule(
                    job.id, next_daily_run(*daily, after=now)
                )
            else:
                await self.store.remove(job.id)
            handler = self._handlers.get(job.kind)
            if handler is None:
                logger.warning(f"job#{job.id} kind={job.kind} 没有注册 handler，跳过")
                continue
            try:
                await handler(job, channel)
                logger.info(f"job#{job.id} {job.kind} @{job.session} 已执行")
            except Exception:
                logger.exception(f"job#{job.id} {job.kind} 执行失败")
