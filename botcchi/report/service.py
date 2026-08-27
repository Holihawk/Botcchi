"""日报服务：按日缓存出图、daily_report 定时推送、get_daily_report 工具"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from ..channels.base import Channel, SessionKey
from ..config import RenderSettings
from ..render import SvgRenderer, to_onebot_file
from ..scheduler import TZ, JobStore, ScheduledJob, Scheduler, next_daily_run
from ..tools.registry import Tool, ToolContext, tool
from .card import FONT_DIR, build_report_card
from .data import fetch_report_data

if TYPE_CHECKING:
    from ..perms import PermissionGate

KIND_DAILY_REPORT = "daily_report"
RENDER_ZOOM = 1.5
"""日报很长且含多张封面图，用 1.5 倍渲染平衡清晰度与体积"""


class ReportSchedules:
    """每群一条 daily_report job。"""

    def __init__(self, jobs: JobStore):
        self.jobs = jobs

    @staticmethod
    def _session(group_id: str) -> SessionKey:
        return SessionKey(channel="onebot", chat_type="group", chat_id=group_id)

    async def set(self, group_id: str, hour: int) -> None:
        session = self._session(group_id)
        await self.jobs.remove_matching(KIND_DAILY_REPORT, str(session))
        await self.jobs.add(
            KIND_DAILY_REPORT,
            session,
            {},
            next_daily_run(hour, 0, after=datetime.now(UTC)),
            recurrence=f"daily@{hour:02d}:00",
        )

    async def cancel(self, group_id: str) -> bool:
        removed = await self.jobs.remove_matching(
            KIND_DAILY_REPORT, str(self._session(group_id))
        )
        return removed > 0


class DailyReportService:
    def __init__(
        self,
        renderer: SvgRenderer,
        schedules: ReportSchedules | None = None,
        cache_dir: Path = Path("data/temp/report"),
    ):
        settings: RenderSettings = renderer.settings.model_copy(deep=True)
        settings.font_dirs = [*settings.font_dirs, str(FONT_DIR)]
        self.renderer = SvgRenderer(settings)
        self.schedules = schedules
        self.cache_dir = cache_dir
        self._lock = asyncio.Lock()

    def _cache_file(self) -> Path:
        return self.cache_dir / f"{datetime.now(TZ).date().isoformat()}.png"

    def cached_today(self) -> bool:
        return self._cache_file().exists()

    async def report_png(self) -> bytes:
        """今日日报（当日首次调用抓数据渲染并缓存，其后直接读盘）"""
        async with self._lock:
            file = self._cache_file()
            if file.exists():
                return file.read_bytes()
            data = await fetch_report_data(datetime.now(TZ))
            png = await self.renderer.render(
                build_report_card(data), zoom=RENDER_ZOOM
            )
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            for old in self.cache_dir.glob("*.png"):
                old.unlink(missing_ok=True)
            file.write_bytes(png)
            logger.info(f"孤独日报已生成: {file}（{len(png) // 1024}KB）")
            return png


def register_report_job(scheduler: Scheduler, service: DailyReportService) -> None:
    async def handle(job: ScheduledJob, channel: Channel) -> None:
        png = await service.report_png()
        await channel.send_image(job.session, to_onebot_file(png))

    scheduler.register(KIND_DAILY_REPORT, handle)


def build_report_tool(service: DailyReportService) -> Tool:
    @tool(
        name="get_daily_report",
        description=(
            "生成并发送今天的「孤独日报」图片到当前聊天：节日倒数、B站热搜、"
            "今日新番、60秒读世界、IT资讯、今日一言。用户想看日报/新闻/"
            "今天发生了什么时调用。发送后简短回应即可，不要复述内容。"
        ),
    )
    async def get_daily_report(ctx: ToolContext) -> str:
        try:
            png = await service.report_png()
            await ctx.channel.send_image(
                ctx.incoming.session, to_onebot_file(png)
            )
        except NotImplementedError:
            return "当前聊天渠道不支持发送图片"
        except Exception:
            logger.exception("日报生成/发送失败")
            return "日报生成失败了，请告诉用户稍后再试"
        return "孤独日报图片已发送"

    return get_daily_report


def build_report_schedule_tool(
    service: DailyReportService, gate: "PermissionGate"
) -> Tool:
    @tool(
        name="schedule_daily_report",
        description=(
            "设置或取消本群的孤独日报每日定时推送。用户说「每天早上八点发日报」"
            "之类时调用；hour 为 0-23 的整点，cancel=true 表示取消本群定时。"
            "仅群聊可用，需要设置者有群管理权限。把结果如实转告用户。"
        ),
    )
    async def schedule_daily_report(
        ctx: ToolContext, hour: int = 8, cancel: bool = False
    ) -> str:
        session = ctx.incoming.session
        if session.chat_type != "group":
            return "定时日报按群设置，请让用户在群聊中使用"
        if service.schedules is None:
            return "未配置数据库，定时日报不可用"
        # 与「日报定时」指令同一道权限闸：身份取自会话上下文
        if not await gate.can_manage(ctx.incoming):
            return "该用户没有本群的管理权限，不能设置定时日报，请婉拒"
        if cancel:
            removed = await service.schedules.cancel(session.chat_id)
            return "已取消本群的定时日报" if removed else "本群没有设置过定时日报"
        if not 0 <= hour <= 23:
            return "hour 需为 0-23 的整点"
        await service.schedules.set(session.chat_id, hour)
        return f"设置成功：以后每天 {hour} 点向本群推送孤独日报"

    return schedule_daily_report


def build_report_commands(service: DailyReportService | None, gate) -> list:
    """「孤独日报 / 日报定时(取消)」指令。"""
    from ..commands import Command, exact, prefix
    from ..render import to_onebot_file

    async def report_cmd(incoming, channel, rest: str) -> bool:
        session = incoming.session
        if service is None:
            return True
        if not service.cached_today():
            await channel.send_text(session, "小孤独在赶稿了，稍等一下…")
        try:
            png = await service.report_png()
            await channel.send_image(session, to_onebot_file(png))
        except NotImplementedError:
            await channel.send_text(session, "这里发不了图片哦")
        except Exception:
            logger.exception("孤独日报生成失败")
            await channel.send_text(session, "日报出不来了…稍后再试试吧")
        return True

    async def cancel_cmd(incoming, channel, rest: str) -> bool:
        if incoming.session.chat_type != "group" or service is None:
            return True
        if not await gate.can_manage(incoming):
            await channel.send_text(incoming.session, "只有管理者能设置定时日报哦")
            return True
        if service.schedules is None:
            await channel.send_text(
                incoming.session, "未配置数据库，定时日报不可用"
            )
            return True
        removed = await service.schedules.cancel(incoming.session.chat_id)
        await channel.send_text(
            incoming.session,
            "已取消本群的定时日报" if removed else "本群没有设置定时日报",
        )
        return True

    async def schedule_cmd(incoming, channel, rest: str) -> bool:
        session = incoming.session
        if session.chat_type != "group":
            await channel.send_text(session, "请在群聊中使用日报定时")
            return True
        if service is None or service.schedules is None:
            await channel.send_text(session, "未配置数据库，定时日报不可用")
            return True
        if not await gate.can_manage(incoming):
            await channel.send_text(session, "只有管理者能设置定时日报哦")
            return True
        parts = rest.split()
        if not parts or not parts[0].isdigit() or not 0 <= int(parts[0]) <= 23:
            await channel.send_text(
                session, "用法：日报定时 <小时0-23>，如「日报定时 8」"
            )
            return True
        hour = int(parts[0])
        await service.schedules.set(session.chat_id, hour)
        await channel.send_text(
            session, f"好啦，以后每天 {hour} 点准时送上孤独日报"
        )
        return True

    return [
        Command(exact("孤独日报"), report_cmd),
        Command(prefix("日报定时取消"), cancel_cmd),
        Command(prefix("日报定时"), schedule_cmd),
    ]
