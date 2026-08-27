"""孤独日报：农历/节日计算、卡片构建、按日缓存、定时推送、jobs API。"""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_service import FakeChannel, make_settings

from botcchi.app import build_context, create_app
from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.report.card import build_report_card
from botcchi.report.data import (
    ReportData,
    festival_countdowns,
    lunar_display,
)
from botcchi.report.service import KIND_DAILY_REPORT, DailyReportService


class ImageChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.images: list[tuple[SessionKey, str]] = []

    async def send_image(self, session, file):
        self.images.append((session, file))


def fake_data() -> ReportData:
    return ReportData(
        day=date(2026, 7, 18),
        week="六",
        zh_date="六月初五",
        festivals=[(69, "中秋节"), (75, "国庆节")],
        hitokoto="今天也要加油",
        bili=["热搜<A>", "热搜B"],
        six=["新闻一", "新闻二"],
        it=["IT一"],
        anime=[("孤独摇滚", None), ("轻音少女", None)],
    )


def test_lunar_and_festivals():
    assert lunar_display(date(2026, 7, 18)) == "六月初五"
    items = festival_countdowns(date(2026, 7, 18))
    assert [n for _, n in items[:2]] == ["中秋节", "国庆节"]
    assert {n: d for d, n in items}["中秋节"] == 69
    assert all(d >= 0 for d, _ in items) and len(items) == 7


def test_card_contains_all_sections():
    svg = build_report_card(fake_data())
    for expected in (
        "孤独日报", "BOCCHI NEWS", "摸鱼日历", "B站热点", "今日新番",
        "60S读世界", "IT资讯", "今日一言", "中秋节", "六月初五",
        "孤独摇滚", "新闻一", "今天也要加油", "暂无封面",
    ):
        assert expected in svg, expected
    assert "热搜<A>" not in svg and "热搜&lt;A&gt;" in svg  # 转义


async def test_report_service_caches_per_day(tmp_path, monkeypatch):
    calls = []

    async def fake_fetch(now=None):
        calls.append(1)
        return fake_data()

    monkeypatch.setattr("botcchi.report.service.fetch_report_data", fake_fetch)
    from botcchi.config import RenderSettings
    from botcchi.render import SvgRenderer

    service = DailyReportService(
        SvgRenderer(RenderSettings()), cache_dir=tmp_path / "report"
    )
    assert not service.cached_today()
    png1 = await service.report_png()
    assert png1[:8] == b"\x89PNG\r\n\x1a\n"
    assert service.cached_today()
    png2 = await service.report_png()
    assert png1 == png2 and len(calls) == 1  # 当日只抓一次数据


async def test_schedule_and_push(tmp_path, mock_openai, monkeypatch):
    async def fake_fetch(now=None):
        return fake_data()

    monkeypatch.setattr("botcchi.report.service.fetch_report_data", fake_fetch)

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    settings.onebot.superusers = ["10001"]
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()
    ctx.service.report.cache_dir = tmp_path / "report"

    channel = ImageChannel()
    group = SessionKey(channel="fake", chat_type="group", chat_id="777")

    def msg(text: str) -> IncomingMessage:
        return IncomingMessage(
            session=group, user_id="10001", user_name="hhk", text=text, at_me=False
        )

    # 指令直达出图
    await ctx.service.handle(msg("孤独日报"), channel)
    assert len(channel.images) == 1
    assert channel.images[0][1].startswith("base64://")

    # 设定时 → job 落库
    await ctx.service.handle(msg("日报定时 8"), channel)
    (job,) = await ctx.jobs.list_jobs(kind=KIND_DAILY_REPORT)
    assert job.recurrence == "daily@08:00" and job.session.chat_id == "777"

    # 到点推送
    await ctx.jobs.reschedule(job.id, datetime.now(UTC) - timedelta(minutes=1))
    await ctx.scheduler._tick(channel)
    assert len(channel.images) == 2

    # 取消
    await ctx.service.handle(msg("日报定时取消"), channel)
    assert await ctx.jobs.list_jobs(kind=KIND_DAILY_REPORT) == []
    await ctx.shutdown()


async def test_report_tool(tmp_path, monkeypatch):
    async def fake_fetch(now=None):
        return fake_data()

    monkeypatch.setattr("botcchi.report.service.fetch_report_data", fake_fetch)
    from botcchi.config import RenderSettings
    from botcchi.render import SvgRenderer
    from botcchi.report.service import build_report_tool
    from botcchi.tools.registry import ToolContext

    service = DailyReportService(
        SvgRenderer(RenderSettings()), cache_dir=tmp_path / "report"
    )
    tool = build_report_tool(service)
    assert tool.needs_context

    channel = ImageChannel()
    session = SessionKey(channel="fake", chat_type="private", chat_id="10001")
    ctx = ToolContext(
        incoming=IncomingMessage(
            session=session, user_id="10001", user_name="hhk", text="", at_me=True
        ),
        channel=channel,
        bot_name="波奇",
    )
    assert "已发送" in await tool.execute("", ctx)
    assert len(channel.images) == 1


def test_jobs_api(tmp_path, mock_openai):
    import asyncio

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"

    # 先用独立事件循环种一条任务（模拟已有的用户提醒）
    async def seed():
        ctx = build_context(settings, client=mock_openai.client())
        await ctx.db.init()
        await ctx.jobs.add(
            "reminder",
            SessionKey(channel="onebot", chat_type="private", chat_id="10001"),
            {"message": "喝水"},
            datetime.now(UTC) + timedelta(hours=1),
            created_by="10001",
        )
        await ctx.shutdown()

    asyncio.run(seed())

    app = create_app(settings, client=mock_openai.client())
    with TestClient(app) as client:
        jobs = client.get("/api/jobs").json()
        assert len(jobs) == 1
        job = jobs[0]
        assert job["kind"] == "reminder" and job["payload"]["message"] == "喝水"
        assert job["created_by"] == "10001"
        assert job["session_key"] == "onebot:private:10001"
        assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
        assert client.get("/api/jobs").json() == []
        assert client.delete("/api/jobs/999").status_code == 404


@pytest.mark.parametrize("year,expected", [(2026, 5), (2025, 4)])
def test_tomb_sweeping_formula(year, expected):
    from botcchi.report.data import _tomb_sweeping_day

    assert _tomb_sweeping_day(year) == date(year, 4, expected)
