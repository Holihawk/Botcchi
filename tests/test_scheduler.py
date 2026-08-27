"""统一定时任务调度器：job 存取、每日顺延、一次性移除、handler 容错。"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from botcchi.channels.base import SessionKey
from botcchi.db.engine import Database
from botcchi.scheduler import (
    JobStore,
    Scheduler,
    next_daily_run,
    parse_daily,
)

TZ = ZoneInfo("Asia/Shanghai")
SESSION = SessionKey(channel="onebot", chat_type="group", chat_id="777")


@pytest.fixture
async def store(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/jobs.db")
    await db.init()
    yield JobStore(db)
    await db.aclose()


class FakeChannel:
    name = "fake"

    async def send_text(self, session, text, *, reply_to=None, at_user=None):
        pass


def test_session_key_roundtrip():
    assert SessionKey.parse(str(SESSION)) == SESSION


def test_parse_daily():
    assert parse_daily("daily@22:00") == (22, 0)
    assert parse_daily("daily@8:30") == (8, 30)
    assert parse_daily("daily@25:00") is None
    assert parse_daily("cron@* * *") is None


def test_next_daily_run():
    # 本地 2026-07-18 10:00 → 当天 22:00
    after = datetime(2026, 7, 18, 10, 0, tzinfo=TZ)
    nxt = next_daily_run(22, 0, after=after)
    assert nxt.astimezone(TZ) == datetime(2026, 7, 18, 22, 0, tzinfo=TZ)
    # 已过 22:00 → 明天
    after = datetime(2026, 7, 18, 22, 0, tzinfo=TZ)
    nxt = next_daily_run(22, 0, after=after)
    assert nxt.astimezone(TZ) == datetime(2026, 7, 19, 22, 0, tzinfo=TZ)


async def test_store_add_due_remove(store):
    past = datetime.now(UTC) - timedelta(minutes=1)
    future = datetime.now(UTC) + timedelta(hours=1)
    id1 = await store.add("reminder", SESSION, {"text": "喝水"}, past)
    await store.add("reminder", SESSION, {"text": "开会"}, future)

    due = await store.due()
    assert [j.id for j in due] == [id1]
    assert due[0].payload == {"text": "喝水"} and due[0].session == SESSION

    assert len(await store.list_jobs(kind="reminder")) == 2
    assert await store.remove(id1) is True
    assert await store.remove(id1) is False
    assert len(await store.list_jobs()) == 1

    removed = await store.remove_matching("reminder", str(SESSION))
    assert removed == 1


async def test_scheduler_oneshot_and_daily(store):
    fired: list[str] = []
    scheduler = Scheduler(store)

    async def handler(job, channel):
        fired.append(job.payload["text"])

    scheduler.register("reminder", handler)

    past = datetime.now(UTC) - timedelta(minutes=1)
    await store.add("reminder", SESSION, {"text": "一次性"}, past)
    await store.add(
        "reminder", SESSION, {"text": "每天"}, past, recurrence="daily@09:00"
    )

    await scheduler._tick(FakeChannel())
    assert sorted(fired) == ["一次性", "每天"]

    jobs = await store.list_jobs()
    assert len(jobs) == 1  # 一次性已移除，每日的顺延保留
    assert jobs[0].next_run_at > datetime.now(UTC)
    assert jobs[0].next_run_at.astimezone(TZ).hour == 9

    await scheduler._tick(FakeChannel())
    assert len(fired) == 2  # 未到期不重复执行


async def test_scheduler_tolerates_failures(store):
    fired: list[str] = []
    scheduler = Scheduler(store)

    async def bad_handler(job, channel):
        raise RuntimeError("执行崩了")

    async def good_handler(job, channel):
        fired.append(job.payload["text"])

    scheduler.register("bad", bad_handler)
    scheduler.register("good", good_handler)

    past = datetime.now(UTC) - timedelta(minutes=1)
    await store.add("bad", SESSION, {"text": "x"}, past)
    await store.add("unknown_kind", SESSION, {"text": "y"}, past)
    await store.add("good", SESSION, {"text": "ok"}, past)

    await scheduler._tick(FakeChannel())
    assert fired == ["ok"]  # 崩溃与未注册的 kind 不影响其他任务
    assert await store.list_jobs() == []  # 一次性任务执行前已移除，不会热循环
