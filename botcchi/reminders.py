"""自然语言提醒：「2小时后提醒我喝水」「明早九点提醒我开会」。

分工：LLM 负责把人话翻译成结构化参数（相对分钟数或绝对时间二选一），
算法负责校验（必须是未来、限 30 天内、限每人条数）、落库与到点投递。
设置成功会回显解析出的时间，用户可当场发现换算错误。

到点投递：群聊 @ 用户；文案由当前会话绑定的人格现场生成（LLM 失败时
回退固定文案）。
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from .agent.context import build_system_message
from .agent.events import TurnComplete
from .agent.messages import ChatMessage
from .channels.base import Channel, IncomingMessage
from .scheduler import (
    TZ,
    JobStore,
    ScheduledJob,
    Scheduler,
    next_daily_run,
    parse_daily,
)
from .tools.registry import Tool, ToolContext, tool

if TYPE_CHECKING:
    from .service import ChatService

KIND_REMINDER = "reminder"
MAX_PER_USER = 10
MAX_DAYS_AHEAD = 30


def _fmt_local(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M")


class ReminderService:
    def __init__(self, jobs: JobStore):
        self.jobs = jobs

    async def list_for(self, user_id: str) -> list[ScheduledJob]:
        return await self.jobs.list_jobs(kind=KIND_REMINDER, created_by=user_id)

    async def create(
        self,
        ctx: ToolContext,
        message: str,
        run_at: datetime,
        recurrence: str | None = None,
    ) -> int:
        return await self.jobs.add(
            KIND_REMINDER,
            ctx.incoming.session,
            {"message": message, "user_name": ctx.incoming.user_name},
            run_at,
            recurrence=recurrence,
            created_by=ctx.incoming.user_id,
        )

    async def cancel(self, user_id: str, reminder_id: int) -> bool:
        """只能取消自己的提醒"""
        mine = {job.id for job in await self.list_for(user_id)}
        if reminder_id not in mine:
            return False
        return await self.jobs.remove(reminder_id)

    async def cancel_all(self, user_id: str) -> int:
        removed = 0
        for job in await self.list_for(user_id):
            removed += await self.jobs.remove(job.id)
        return removed


async def _persona_text(
    chat: "ChatService", job: ScheduledJob, name: str, message: str
) -> str:
    """用会话绑定的人格生成提醒文案；失败回退固定文案"""
    fallback = f"提醒时间到：{message}"
    try:
        assistant = await chat.resolve_assistant(job.session)
        incoming = IncomingMessage(
            session=job.session,
            user_id=job.created_by,
            user_name=name,
            text="",
        )
        instruction = (
            f"到了你之前答应提醒用户「{name}」的时间，"
            f"提醒事项：「{message}」。"
            "请用你的语气把这条提醒说出来：一两句话，"
            "必须明确包含提醒事项本身，直接输出要发送的内容。"
        )
        messages = [
            build_system_message(assistant, incoming),
            ChatMessage(role="user", content=instruction),
        ]
        final: TurnComplete | None = None
        async for event in chat.loop.run(assistant, messages):
            if isinstance(event, TurnComplete):
                final = event
        from .service import clean_reply

        text = clean_reply(final.message.text()) if final else ""
        return text or fallback
    except Exception:
        logger.exception("提醒文案生成失败，使用默认文案")
        return fallback


def register_reminder_job(scheduler: Scheduler, chat: "ChatService") -> None:
    async def handle(job: ScheduledJob, channel: Channel) -> None:
        name = job.payload.get("user_name", "")
        message = job.payload.get("message", "")
        text = await _persona_text(chat, job, name, message)
        at_user = job.created_by if job.session.chat_type == "group" else None
        await channel.send_text(job.session, text, at_user=at_user)

    scheduler.register(KIND_REMINDER, handle)


def build_reminder_tools(service: ReminderService) -> list[Tool]:
    @tool(
        name="set_reminder",
        description=(
            "为当前用户设置定时提醒，到点后在当前聊天里提醒ta。"
            "相对时间用 delay_minutes（如「2小时后」传 120），"
            "绝对时间用 at（北京时间，格式 YYYY-MM-DD HH:MM，"
            "结合当前时间把「明天」「周五」等换算成具体日期），二者选一。"
            "daily=true 表示每天该时刻重复提醒（此时只看 at 的时分）。"
            "message 填提醒内容本身，不要包含时间。"
            "设置成功后向用户复述返回的时间，便于确认换算无误。"
        ),
    )
    async def set_reminder(
        ctx: ToolContext,
        message: str,
        at: str = "",
        delay_minutes: int = 0,
        daily: bool = False,
    ) -> str:
        message = message.strip()
        if not message:
            return "设置失败：提醒内容不能为空"
        if bool(at) == (delay_minutes > 0):
            return "设置失败：at 和 delay_minutes 必须二选一"
        if daily and not at:
            return "设置失败：每日重复提醒需要用 at 指定时刻"
        if len(await service.list_for(ctx.incoming.user_id)) >= MAX_PER_USER:
            return f"设置失败：待办提醒已达 {MAX_PER_USER} 条上限，请先取消一些"

        now = datetime.now(UTC)
        if delay_minutes > 0:
            if delay_minutes > MAX_DAYS_AHEAD * 24 * 60:
                return f"设置失败：最多只能提前 {MAX_DAYS_AHEAD} 天设置提醒"
            run_at = now + timedelta(minutes=delay_minutes)
            recurrence = None
        else:
            try:
                local = datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            except ValueError:
                return (
                    f"设置失败：at 格式应为 YYYY-MM-DD HH:MM（收到「{at}」）。"
                    f"当前北京时间是 {_fmt_local(now)}"
                )
            if daily:
                recurrence = f"daily@{local.hour:02d}:{local.minute:02d}"
                run_at = next_daily_run(local.hour, local.minute, after=now)
            else:
                recurrence = None
                run_at = local.astimezone(UTC)
                if run_at <= now:
                    return (
                        f"设置失败：{at} 已经过去了，"
                        f"当前北京时间是 {_fmt_local(now)}，请重新换算"
                    )
                if run_at > now + timedelta(days=MAX_DAYS_AHEAD):
                    return f"设置失败：最多只能提前 {MAX_DAYS_AHEAD} 天设置提醒"

        reminder_id = await service.create(ctx, message, run_at, recurrence)
        when = _fmt_local(run_at) + ("，每天重复" if daily else "")
        return f"已设置提醒 #{reminder_id}：{when}「{message}」"

    @tool(
        name="list_reminders",
        description="查看当前用户的待办提醒列表（含编号，取消时要用）",
    )
    async def list_reminders(ctx: ToolContext) -> str:
        jobs = await service.list_for(ctx.incoming.user_id)
        if not jobs:
            return "当前没有待办提醒"
        lines = []
        for job in jobs:
            daily = parse_daily(job.recurrence or "")
            when = (
                f"每天 {daily[0]:02d}:{daily[1]:02d}"
                if daily
                else _fmt_local(job.next_run_at)
            )
            lines.append(f"#{job.id} {when} 「{job.payload.get('message', '')}」")
        return "待办提醒：\n" + "\n".join(lines)

    @tool(
        name="cancel_reminder",
        description=(
            "取消当前用户的提醒。reminder_id 为 list_reminders 里的编号；"
            "传 0 取消该用户的全部提醒。只能取消用户自己的提醒。"
        ),
    )
    async def cancel_reminder(ctx: ToolContext, reminder_id: int = 0) -> str:
        user_id = ctx.incoming.user_id
        if reminder_id <= 0:
            removed = await service.cancel_all(user_id)
            return f"已取消 {removed} 条提醒" if removed else "当前没有待办提醒"
        if await service.cancel(user_id, reminder_id):
            return f"已取消提醒 #{reminder_id}"
        return f"取消失败：没有找到属于该用户的提醒 #{reminder_id}"

    return [set_reminder, list_reminders, cancel_reminder]
