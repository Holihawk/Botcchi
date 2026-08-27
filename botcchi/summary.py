"""群聊总结服务：从归档取消息 → LLM 生成总结；定时总结挂在统一调度器上"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from .agent.events import TurnComplete
from .agent.messages import system, user
from .channels.base import Channel, SessionKey
from .config import SummarySettings
from .db.archive import MessageArchive
from .providers.registry import ProviderRegistry
from .scheduler import JobStore, ScheduledJob, Scheduler, next_daily_run
from .tools.registry import Tool, ToolContext, tool

if TYPE_CHECKING:
    from .perms import PermissionGate

KIND_GROUP_SUMMARY = "group_summary"


class SummarySchedules:
    """定时总结配置 = 统一调度器上的 group_summary 类 job（每群一条）"""

    def __init__(self, jobs: JobStore):
        self.jobs = jobs

    @staticmethod
    def _session(group_id: str) -> SessionKey:
        return SessionKey(channel="onebot", chat_type="group", chat_id=group_id)

    async def set(self, group_id: str, hour: int, min_messages: int) -> None:
        session = self._session(group_id)
        await self.jobs.remove_matching(KIND_GROUP_SUMMARY, str(session))
        await self.jobs.add(
            KIND_GROUP_SUMMARY,
            session,
            {"min_messages": min_messages},
            next_daily_run(hour, 0, after=datetime.now(UTC)),
            recurrence=f"daily@{hour:02d}:00",
        )

    async def cancel(self, group_id: str) -> bool:
        removed = await self.jobs.remove_matching(
            KIND_GROUP_SUMMARY, str(self._session(group_id))
        )
        return removed > 0

SUMMARY_PROMPT = """\
你是群聊记录的总结助手。根据给出的聊天记录，用中文输出一段结构清晰的总结：
- 按话题分组，每个话题一两句话，标注主要参与者（用昵称，不要带QQ号）
- 保留有信息量的结论、约定、梗；忽略无意义的闲聊和刷屏
- 语气轻松一点，总长度控制在 400 字以内
- 只输出总结内容本身，不要开场白和结尾"""


class GroupSummaryService:
    def __init__(
        self,
        settings: SummarySettings,
        archive: MessageArchive,
        schedules: SummarySchedules,
        providers: ProviderRegistry,
        default_model: str,
    ):
        self.settings = settings
        self.archive = archive
        self.schedules = schedules
        self.providers = providers
        self.default_model = default_model
        self._cooldown: dict[str, float] = {}

    @property
    def model_ref(self) -> str:
        return self.settings.model or self.default_model

    def in_cooldown(self, group_id: str) -> bool:
        last = self._cooldown.get(group_id)
        now = asyncio.get_event_loop().time()
        if last is not None and now - last < self.settings.cooldown_seconds:
            return True
        self._cooldown[group_id] = now
        return False

    async def summarize(
        self, group_id: str, count: int, focus: str | None = None
    ) -> str:
        count = max(20, min(count, self.settings.max_count))
        messages = await self.archive.recent(group_id, count)
        if len(messages) < 10:
            return "群里最近的消息太少了，攒一攒再总结吧"
        transcript = "\n".join(
            f"[{m.created_at:%H:%M}] {m.user_name}: {m.text}" for m in messages
        )
        prompt = SUMMARY_PROMPT
        if focus:
            prompt += f"\n- 本次重点关注与「{focus}」相关的内容"
        provider, model, _ = self.providers.resolve(self.model_ref)
        final: TurnComplete | None = None
        async for event in provider.stream_chat(
            model=model,
            messages=[system(prompt), user(transcript)],
            temperature=0.4,
        ):
            if isinstance(event, TurnComplete):
                final = event
        text = final.message.text().strip() if final else ""
        if not text:
            return "总结失败了，稍后再试试吧"
        header = f"最近 {len(messages)} 条消息的总结：\n"
        return header + text


def register_summary_job(
    scheduler: Scheduler, service: GroupSummaryService
) -> None:
    """把定时总结注册为调度器的 group_summary 执行器"""

    async def handle(job: ScheduledJob, channel: Channel) -> None:
        group_id = job.session.chat_id
        min_messages = int(job.payload.get("min_messages", 50))
        if not await _enough_recent_messages(service, group_id, min_messages):
            logger.info(
                f"群 {group_id} 定时总结跳过：24 小时内消息数 < 门槛 {min_messages}"
            )
            return
        text = await service.summarize(group_id, service.settings.default_count)
        await channel.send_text(job.session, f"【今日群聊总结】\n{text}")

    scheduler.register(KIND_GROUP_SUMMARY, handle)


def build_summary_commands(
    service: GroupSummaryService | None,
    gate: "PermissionGate",
    default_count: int,
) -> list:
    """「消息总结 / 总结定时(取消)」指令；service 为 None 时回未配置提示"""
    from .commands import Command, prefix

    async def summarize_cmd(incoming, channel, rest: str) -> bool:
        session = incoming.session
        if session.chat_type != "group":
            await channel.send_text(session, "请在群聊中使用消息总结")
            return True
        if service is None:
            await channel.send_text(session, "未配置数据库，群聊总结不可用")
            return True
        if service.in_cooldown(session.chat_id):
            await channel.send_text(session, "刚总结过啦，过几分钟再来吧")
            return True
        count = default_count
        focus = None
        if rest:
            head, _, tail = rest.partition(" ")
            if head.isdigit():
                count = int(head)
                focus = tail.strip() or None
            else:
                focus = rest
        await channel.send_text(session, "正在翻聊天记录，稍等…")
        try:
            result = await service.summarize(session.chat_id, count, focus)
        except Exception:
            logger.exception("群聊总结失败")
            result = "总结失败了，稍后再试试吧"
        await channel.send_text(session, result)
        return True

    async def cancel_cmd(incoming, channel, rest: str) -> bool:
        if incoming.session.chat_type != "group" or service is None:
            return True
        if not await gate.can_manage(incoming):
            await channel.send_text(
                incoming.session, "只有管理者能设置定时总结哦"
            )
            return True
        removed = await service.schedules.cancel(incoming.session.chat_id)
        await channel.send_text(
            incoming.session,
            "已取消本群的定时总结" if removed else "本群没有设置定时总结",
        )
        return True

    async def schedule_cmd(incoming, channel, rest: str) -> bool:
        session = incoming.session
        if session.chat_type != "group":
            await channel.send_text(session, "请在群聊中使用总结定时")
            return True
        if service is None:
            await channel.send_text(session, "未配置数据库，群聊总结不可用")
            return True
        if not await gate.can_manage(incoming):
            await channel.send_text(session, "只有管理者能设置定时总结哦")
            return True
        parts = rest.split()
        if not parts or not parts[0].isdigit() or not 0 <= int(parts[0]) <= 23:
            await channel.send_text(
                session,
                "用法：总结定时 <小时0-23> [最少消息数]，如「总结定时 22 50」",
            )
            return True
        hour = int(parts[0])
        min_messages = (
            int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 50
        )
        await service.schedules.set(session.chat_id, hour, min_messages)
        await channel.send_text(
            session,
            f"好啦，每天 {hour} 点消息数满 {min_messages} 条就自动总结",
        )
        return True

    return [
        Command(prefix("消息总结"), summarize_cmd),
        Command(prefix("总结定时取消"), cancel_cmd),
        Command(prefix("总结定时"), schedule_cmd),
    ]


def build_summarize_tool(service: GroupSummaryService) -> Tool:
    @tool(
        name="summarize_group",
        description=(
            "立即总结当前群最近的聊天记录并返回总结文本。用户说「总结一下"
            "群里最近聊了什么」之类时调用；count 为回看的消息条数"
            "（默认 200），focus 为可选的关注主题。仅群聊可用，"
            "生成需要一些时间。把总结内容转述给用户即可。"
        ),
    )
    async def summarize_group(
        ctx: ToolContext, count: int = 200, focus: str = ""
    ) -> str:
        session = ctx.incoming.session
        if session.chat_type != "group":
            return "群聊总结只能在群里使用，请告知用户"
        # 与「消息总结」指令共享同一份冷却记账
        if service.in_cooldown(session.chat_id):
            return "刚总结过（冷却中），请让用户过几分钟再试"
        return await service.summarize(session.chat_id, count, focus or None)

    return summarize_group


def build_summary_schedule_tool(
    service: GroupSummaryService, gate: "PermissionGate"
) -> Tool:
    @tool(
        name="schedule_group_summary",
        description=(
            "设置或取消本群的每日定时群聊总结。用户说「每天晚上十点总结群聊」"
            "之类时调用；hour 为 0-23 的整点，min_messages 为触发总结的最少"
            "消息数（默认 50），cancel=true 表示取消。仅群聊可用，"
            "需要设置者有群管理权限。把结果如实转告用户。"
        ),
    )
    async def schedule_group_summary(
        ctx: ToolContext,
        hour: int = 22,
        min_messages: int = 50,
        cancel: bool = False,
    ) -> str:
        session = ctx.incoming.session
        if session.chat_type != "group":
            return "定时总结按群设置，请让用户在群聊中使用"
        # 与「总结定时」指令同一道权限闸：身份取自会话上下文
        if not await gate.can_manage(ctx.incoming):
            return "该用户没有本群的管理权限，不能设置定时总结，请婉拒"
        if cancel:
            removed = await service.schedules.cancel(session.chat_id)
            return "已取消本群的定时总结" if removed else "本群没有设置过定时总结"
        if not 0 <= hour <= 23:
            return "hour 需为 0-23 的整点"
        await service.schedules.set(session.chat_id, hour, max(1, min_messages))
        return (
            f"设置成功：每天 {hour} 点，24 小时内消息数满 "
            f"{max(1, min_messages)} 条就自动总结本群"
        )

    return schedule_group_summary


async def _enough_recent_messages(
    service: GroupSummaryService, group_id: str, min_messages: int
) -> bool:
    """最近 min_messages 条消息是否都发生在 24 小时内（即活跃度达标）。

    时间比较在 Python 侧完成，规避 SQLite 无时区列的比较陷阱。
    """
    rows = await service.archive.recent(group_id, min_messages)
    if len(rows) < min_messages:
        return False
    oldest = rows[0].created_at
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    return oldest >= datetime.now(UTC) - timedelta(hours=24)
