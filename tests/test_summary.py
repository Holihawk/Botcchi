"""消息归档 + 群聊总结：归档钩子、总结指令、定时调度。"""

import json
from datetime import UTC, datetime

from conftest import text_chunks
from fastapi.testclient import TestClient
from test_service import make_settings

from botcchi.app import create_app
from botcchi.channels.base import SessionKey
from botcchi.summary import KIND_GROUP_SUMMARY


def db_settings(tmp_path, **summary_overrides):
    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/summary.db"
    for k, v in summary_overrides.items():
        setattr(settings.summary, k, v)
    return settings


def wait_archived(db_path, expected: int, timeout: float = 5.0) -> None:
    import sqlite3
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with sqlite3.connect(db_path) as conn:
                (count,) = conn.execute(
                    "SELECT COUNT(*) FROM group_messages"
                ).fetchone()
            if count >= expected:
                return
        except sqlite3.OperationalError:
            pass
        time.sleep(0.02)
    raise TimeoutError(f"归档消息未达到 {expected} 条")


def group_event(user_id: int, nickname: str, text: str, msg_id: int) -> str:
    return json.dumps(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 777,
            "user_id": user_id,
            "message_id": msg_id,
            "sender": {"nickname": nickname},
            "message": [{"type": "text", "data": {"text": text}}],
        }
    )


def test_archive_and_summary_command(mock_openai, tmp_path):
    settings = db_settings(tmp_path, cooldown_seconds=0.0)
    app = create_app(settings, client=mock_openai.client())

    with TestClient(app) as client:
        with client.websocket_connect(
            "/onebot/v11/ws", headers={"x-self-id": "999"}
        ) as ws:
            # 灌入 12 条普通群消息（不触发回复：无 @、概率 0）
            for i in range(12):
                ws.send_text(
                    group_event(100 + i, f"用户{i}", f"今天聊点什么呢 {i}", i + 1)
                )
            # WS 事件异步落库：轮询 sqlite 直到 12 条归档完成
            wait_archived(tmp_path / "summary.db", 12)

            # 手动总结
            mock_openai.enqueue(text_chunks("大家在讨论今天聊什么"))
            ws.send_text(group_event(100, "用户0", "消息总结 100", 99))

            progress = json.loads(ws.receive_text())
            assert "翻聊天记录" in progress["params"]["message"][-1]["data"]["text"]
            ws.send_text(json.dumps({"status": "ok", "echo": progress["echo"]}))

            result = json.loads(ws.receive_text())
            body = result["params"]["message"][-1]["data"]["text"]
            assert "最近 12 条消息的总结" in body
            assert "大家在讨论今天聊什么" in body
            ws.send_text(json.dumps({"status": "ok", "echo": result["echo"]}))

        # 总结请求携带了归档消息与昵称
        transcript = mock_openai.requests[0]["messages"][1]["content"]
        assert "用户3: 今天聊点什么呢 3" in transcript


def test_summary_needs_messages(mock_openai, tmp_path):
    settings = db_settings(tmp_path, cooldown_seconds=0.0)
    app = create_app(settings, client=mock_openai.client())
    with TestClient(app) as client:
        with client.websocket_connect(
            "/onebot/v11/ws", headers={"x-self-id": "999"}
        ) as ws:
            ws.send_text(group_event(100, "甲", "消息总结", 1))
            progress = json.loads(ws.receive_text())
            ws.send_text(json.dumps({"status": "ok", "echo": progress["echo"]}))
            resp = json.loads(ws.receive_text())
            assert "太少" in resp["params"]["message"][-1]["data"]["text"]
            # LLM 未被调用
            assert mock_openai.requests == []


async def test_schedule_becomes_job(mock_openai, tmp_path):
    """总结定时配置 = 调度器上的 group_summary 每日 job（每群一条）。"""
    from botcchi.app import build_context

    settings = db_settings(tmp_path)
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.startup()
    try:
        schedules = ctx.service.summary.schedules
        await schedules.set("777", hour=22, min_messages=5)

        jobs = await ctx.jobs.list_jobs(kind=KIND_GROUP_SUMMARY)
        assert len(jobs) == 1
        job = jobs[0]
        assert job.session.chat_id == "777"
        assert job.recurrence == "daily@22:00"
        assert job.payload == {"min_messages": 5}

        # 重设覆盖旧 job 而不是叠加
        await schedules.set("777", hour=8, min_messages=10)
        jobs = await ctx.jobs.list_jobs(kind=KIND_GROUP_SUMMARY)
        assert len(jobs) == 1 and jobs[0].recurrence == "daily@08:00"

        assert await schedules.cancel("777") is True
        assert await schedules.cancel("777") is False
        assert await ctx.jobs.list_jobs(kind=KIND_GROUP_SUMMARY) == []
    finally:
        await ctx.shutdown()


async def test_scheduler_sends_summary(mock_openai, tmp_path):
    """统一调度器 tick：到点、消息达标 → 生成并发送，随后顺延到明天。"""
    from datetime import UTC as _UTC
    from datetime import timedelta

    from botcchi.app import build_context

    settings = db_settings(tmp_path)
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.startup()
    try:
        archive = ctx.service.summary.archive
        for i in range(15):
            await archive.add("777", str(i), f"用户{i}", f"消息 {i}")
        await ctx.service.summary.schedules.set("777", hour=3, min_messages=5)
        # 把 job 拨到已到期
        job = (await ctx.jobs.list_jobs(kind=KIND_GROUP_SUMMARY))[0]
        await ctx.jobs.reschedule(job.id, datetime.now(_UTC) - timedelta(minutes=1))

        sent: list[tuple[SessionKey, str]] = []

        class FakeChannel:
            name = "fake"

            async def send_text(self, session, text, *, reply_to=None, at_user=None):
                sent.append((session, text))

        mock_openai.enqueue(text_chunks("今天大家很活跃"))
        await ctx.scheduler._tick(FakeChannel())

        assert len(sent) == 1
        session, text = sent[0]
        assert session.chat_id == "777"
        assert "【今日群聊总结】" in text and "今天大家很活跃" in text
        # 已顺延到未来，不会重复发送
        job = (await ctx.jobs.list_jobs(kind=KIND_GROUP_SUMMARY))[0]
        assert job.next_run_at > datetime.now(_UTC)
        await ctx.scheduler._tick(FakeChannel())
        assert len(sent) == 1

        # 消息量不足的群：到点也跳过
        await ctx.service.summary.schedules.set("888", hour=3, min_messages=50)
        job = next(
            j
            for j in await ctx.jobs.list_jobs(kind=KIND_GROUP_SUMMARY)
            if j.session.chat_id == "888"
        )
        await ctx.jobs.reschedule(job.id, datetime.now(_UTC) - timedelta(minutes=1))
        await ctx.scheduler._tick(FakeChannel())
        assert len(sent) == 1
    finally:
        await ctx.shutdown()


def utc_today_start():
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
