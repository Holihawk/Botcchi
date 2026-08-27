"""自然语言提醒：工具校验、时间换算、权限隔离、到点投递。"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.db.engine import Database
from botcchi.reminders import (
    KIND_REMINDER,
    MAX_PER_USER,
    ReminderService,
    build_reminder_tools,
)
from botcchi.scheduler import TZ, JobStore
from botcchi.tools.registry import ToolContext


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/reminders.db")
    await database.init()
    yield database
    await database.aclose()


@pytest.fixture
def jobs(db):
    return JobStore(db)


@pytest.fixture
def service(jobs):
    return ReminderService(jobs)


@pytest.fixture
def tools(service):
    set_r, list_r, cancel_r = build_reminder_tools(service)
    return {"set": set_r, "list": list_r, "cancel": cancel_r}


def make_ctx(channel=None, user_id="10001", user_name="hhk") -> ToolContext:
    session = SessionKey(channel="fake", chat_type="private", chat_id=user_id)
    return ToolContext(
        incoming=IncomingMessage(
            session=session, user_id=user_id, user_name=user_name, text="", at_me=True
        ),
        channel=channel or FakeChannel(),
        bot_name="波奇",
    )


def args(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


async def test_set_reminder_with_delay(tools, jobs):
    out = await tools["set"].execute(
        args(message="喝水", delay_minutes=120), make_ctx()
    )
    assert out.startswith("已设置提醒 #") and "喝水" in out

    (job,) = await jobs.list_jobs(kind=KIND_REMINDER)
    assert job.created_by == "10001"
    assert job.payload["message"] == "喝水" and job.payload["user_name"] == "hhk"
    expected = datetime.now(UTC) + timedelta(minutes=120)
    assert abs((job.next_run_at - expected).total_seconds()) < 5
    assert job.recurrence is None


async def test_set_reminder_with_absolute_time(tools):
    future = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    out = await tools["set"].execute(args(message="开会", at=future), make_ctx())
    assert "已设置提醒" in out and future in out


async def test_set_reminder_validation(tools):
    past = (datetime.now(TZ) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    out = await tools["set"].execute(args(message="x", at=past), make_ctx())
    assert "已经过去了" in out and "当前北京时间" in out  # 回显现在时间供模型重算

    out = await tools["set"].execute(args(message="x", at="明天九点"), make_ctx())
    assert "格式" in out

    out = await tools["set"].execute(args(message="x"), make_ctx())
    assert "二选一" in out
    out = await tools["set"].execute(
        args(message="x", at="2099-01-01 09:00", delay_minutes=5), make_ctx()
    )
    assert "二选一" in out

    out = await tools["set"].execute(
        args(message="x", delay_minutes=60 * 24 * 365), make_ctx()
    )
    assert "30 天" in out

    out = await tools["set"].execute(args(message="  ", delay_minutes=5), make_ctx())
    assert "不能为空" in out


async def test_daily_reminder(tools, jobs):
    out = await tools["set"].execute(
        args(message="吃药", at="2026-01-01 09:00", daily=True), make_ctx()
    )
    assert "每天重复" in out
    (job,) = await jobs.list_jobs(kind=KIND_REMINDER)
    assert job.recurrence == "daily@09:00"


async def test_reminder_cap(tools):
    for i in range(MAX_PER_USER):
        out = await tools["set"].execute(
            args(message=f"事项{i}", delay_minutes=10 + i), make_ctx()
        )
        assert "已设置提醒" in out
    out = await tools["set"].execute(args(message="超了", delay_minutes=5), make_ctx())
    assert "上限" in out


async def test_list_and_cancel_scoped_to_user(tools):
    assert "没有待办" in await tools["list"].execute("", make_ctx())
    out = await tools["set"].execute(args(message="喝水", delay_minutes=5), make_ctx())
    reminder_id = int(out.split("#")[1].split("：")[0])

    listed = await tools["list"].execute("", make_ctx())
    assert f"#{reminder_id}" in listed and "喝水" in listed
    # 别人看不到、也取消不了
    other = make_ctx(user_id="20002", user_name="小虹")
    assert "没有待办" in await tools["list"].execute("", other)
    assert "取消失败" in await tools["cancel"].execute(
        args(reminder_id=reminder_id), other
    )
    # 本人可以取消
    assert "已取消提醒" in await tools["cancel"].execute(
        args(reminder_id=reminder_id), make_ctx()
    )
    assert "没有待办" in await tools["list"].execute("", make_ctx())


async def test_cancel_all(tools):
    for i in range(3):
        await tools["set"].execute(args(message=f"m{i}", delay_minutes=5), make_ctx())
    assert "已取消 3 条" in await tools["cancel"].execute("", make_ctx())


async def test_reminder_fires_with_persona_text_and_at(mock_openai, tmp_path):
    from conftest import text_chunks

    from botcchi.app import build_context

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()
    channel = FakeChannel()
    past = datetime.now(UTC) - timedelta(seconds=1)

    # 群聊提醒：人格化文案 + @ 用户
    group = SessionKey(channel="fake", chat_type="group", chat_id="777")
    await ctx.jobs.add(
        KIND_REMINDER, group, {"message": "喝水", "user_name": "hhk"},
        past, created_by="10001",
    )
    mock_openai.enqueue(text_chunks("那个…到时间喝水了哦！"))
    await ctx.scheduler._tick(channel)
    assert channel.sent[-1][1] == "那个…到时间喝水了哦！"
    assert channel.ats[-1] == "10001"
    assert await ctx.jobs.list_jobs(kind=KIND_REMINDER) == []  # 一次性已移除
    # 生成文案的请求带了人格 system prompt 与提醒事项
    req = mock_openai.requests[-1]
    assert req["messages"][0]["role"] == "system"
    assert "喝水" in req["messages"][-1]["content"]

    # 私聊提醒：不 @
    private = SessionKey(channel="fake", chat_type="private", chat_id="10001")
    await ctx.jobs.add(
        KIND_REMINDER, private, {"message": "开会", "user_name": "hhk"},
        past, created_by="10001",
    )
    mock_openai.enqueue(text_chunks("要去开会啦！"))
    await ctx.scheduler._tick(channel)
    assert "开会" in channel.sent[-1][1] and channel.ats[-1] is None

    # LLM 失败：回退固定文案，提醒仍送达
    await ctx.jobs.add(
        KIND_REMINDER, group, {"message": "吃药", "user_name": "hhk"},
        past, created_by="10001",
    )
    mock_openai.enqueue_status(500)
    await ctx.scheduler._tick(channel)
    assert channel.sent[-1][1] == "提醒时间到：吃药"
    assert channel.ats[-1] == "10001"
    await ctx.shutdown()


async def test_onebot_send_text_with_at(monkeypatch):
    from botcchi.channels.onebot.adapter import OneBotAdapter
    from botcchi.config import OneBotSettings

    adapter = OneBotAdapter(OneBotSettings(), on_message=None)  # type: ignore[arg-type]
    calls: list[dict] = []

    async def fake_call_api(action, params):
        calls.append(params)
        return {"status": "ok"}

    monkeypatch.setattr(adapter, "call_api", fake_call_api)
    group = SessionKey(channel="onebot", chat_type="group", chat_id="777")
    await adapter.send_text(group, "喝水", at_user="10001")
    segments = calls[-1]["message"]
    assert segments[0] == {"type": "at", "data": {"qq": "10001"}}
    assert segments[1] == {"type": "text", "data": {"text": " 喝水"}}

    # 私聊场景 at_user 被忽略
    private = SessionKey(channel="onebot", chat_type="private", chat_id="10001")
    await adapter.send_text(private, "喝水", at_user="10001")
    assert calls[-1]["message"] == [{"type": "text", "data": {"text": "喝水"}}]


async def test_natural_language_reminder_end_to_end(mock_openai, tmp_path):
    from conftest import text_chunks, tool_call_chunks

    from botcchi.app import build_context

    (tmp_path / "test.yaml").write_text(
        "name: test\n"
        "display_name: 波奇\n"
        "model: mock/m\n"
        "prompt: 你是波奇\n"
        "tools: [set_reminder]\n"
        "policy:\n  split: false\n",
        encoding="utf-8",
    )
    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()

    mock_openai.enqueue(
        tool_call_chunks("set_reminder", '{"message": "喝水", "delay_minutes": 120}')
    )
    mock_openai.enqueue(text_chunks("好，两小时后提醒你喝水！"))

    channel = FakeChannel()
    session = SessionKey(channel="fake", chat_type="private", chat_id="10001")
    await ctx.service.handle(
        IncomingMessage(
            session=session, user_id="10001", user_name="hhk",
            text="波奇2小时后提醒我喝水", at_me=True,
        ),
        channel,
    )
    assert any("提醒你喝水" in text for _, text, _ in channel.sent)
    (job,) = await ctx.jobs.list_jobs(kind=KIND_REMINDER)
    assert job.payload["message"] == "喝水" and job.created_by == "10001"
    await ctx.shutdown()
