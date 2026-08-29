"""ChatService：channel 消息 → 触发判定 → agent loop → 拟人化发送。

M2：群窗口感知的伪人模式、长期记忆注入与异步提取、多人格绑定与内置指令。
"""

import asyncio
import re
from typing import Protocol

from loguru import logger

from .agent.assistant import Assistant, OutputPolicy
from .agent.context import (
    build_bym_user_message,
    build_system_message,
    build_user_message,
    persistable_user_message,
)
from .agent.events import TurnComplete
from .agent.loop import AgentLoop
from .agent.messages import ChatMessage
from .channels.base import Channel, IncomingMessage, SessionKey
from .commands import Command, CommandRouter, exact, prefix
from .config import Settings
from .db.archive import MessageArchive
from .games import BlackjackService, DuelService, Game24Service
from .greeting import GreetingService
from .groupadmin import GroupAdminService
from .help import HelpService, build_help_commands
from .imagegen import ImageGenService, build_imagegen_commands
from .linkparse import LinkParseService
from .memory.group_window import GroupWindow, WindowEntry
from .memory.long_term import MemoryService
from .memory.short_term import ConversationStore
from .perms import PermCommands, PermissionGate, feature_for_command
from .pig import PigService, build_pig_commands
from .report import DailyReportService, build_report_commands
from .social.card import SocialCards
from .social.commands import build_social_commands
from .social.likes import LikeService, build_like_commands
from .social.service import SocialService
from .social.shop import ShopService, ToolCharger, build_shop_commands
from .stats import StatsService
from .stickers import StickerService
from .summary import GroupSummaryService, build_summary_commands
from .superuser import SuperuserService
from .tools.registry import ToolContext
from .trigger.engine import TriggerEngine
from .trigger.repeater import Repeater, content_key
from .weather import WeatherService, build_weather_command

_SENTENCE_SPLIT = re.compile(r"[。？！\n]+")
_CLAUSE_SPLIT = re.compile(r"[^，、；,;]*[，、；,;]+|[^，、；,;]+")
"""子句切分：保留子句标点（句中的逗号丢了读起来会喘不过气）"""
SKIP_MARKER = "[skip]"
"""模型主动放弃回复的信号（详见人格提示词「什么时候不回复」）"""
_SKIP = re.compile(r"^[\[\【(]?\s*skip\s*[\]\】)]?[。.!！]*$", re.I)


def is_skip(text: str) -> bool:
    """模型是否选择不回复。标点/全角括号写歪了也认，免得漏判后把标记发出去"""
    return bool(_SKIP.match(text.strip()))

_AT_DIGITS = re.compile(r"@\d{5,12}\s?")
_LEADING_AT = re.compile(r"^(@\S{1,20}\s*)+")
_NAME_PREFIX = re.compile(r"^\[[^\]]{1,40}\]\s*[:：]\s*")


def clean_reply(text: str) -> str:
    """清洗模型输出：去掉 @某人 与对群消息前缀格式的模仿"""
    text = _NAME_PREFIX.sub("", text.strip())
    text = _LEADING_AT.sub("", text)
    text = _AT_DIGITS.sub("", text)
    return text.strip()


def _split_long(part: str, max_chars: int) -> list[str]:
    """按子句标点把过长的句子拆成不超过 max_chars 的片段。

    找不到可断的标点时原样返回——宁可长一点，也不在词中间硬切。
    """
    if len(part) <= max_chars:
        return [part]
    chunks: list[str] = []
    cur = ""
    for clause in _CLAUSE_SPLIT.findall(part):
        if cur and len(cur) + len(clause) > max_chars:
            chunks.append(cur)
            cur = clause
        else:
            cur += clause
    if cur:
        chunks.append(cur)
    return chunks or [part]


def split_reply(text: str, policy: OutputPolicy) -> list[tuple[str, float]]:
    """把回复按句切分为 (文本, 发送前延迟) 序列，模拟打字节奏"""
    has_explicit_break = "\n" in text
    if not policy.split or (
        policy.split_min_chars
        and len(text) < policy.split_min_chars
        and not has_explicit_break
    ):
        return [(text, 0.0)]
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p.strip()]
    if policy.max_chars:
        parts = [c for p in parts for c in _split_long(p, policy.max_chars)]
    # 启用短回复阈值时，按总长度动态收紧段数，避免中等长度回复总是发满上限。
    segment_limit = policy.max_segments
    if (
        segment_limit
        and policy.split_min_chars
        and policy.max_chars
        and not has_explicit_break
    ):
        estimated = max(1, (len(text) + policy.max_chars - 1) // policy.max_chars)
        segment_limit = min(segment_limit, estimated)
    if segment_limit and len(parts) > segment_limit:
        head = parts[: segment_limit - 1]
        tail = "，".join(parts[segment_limit - 1 :])
        parts = [*head, tail]
    return [
        (p, min(len(p) * policy.delay_per_char, policy.max_delay)) for p in parts
    ] or [(text, 0.0)]


class PersonaBindings(Protocol):
    async def get(self, session: SessionKey) -> str | None: ...
    async def set(self, session: SessionKey, assistant: str) -> None: ...
    async def clear(self, session: SessionKey) -> None: ...


class ChatService:
    def __init__(
        self,
        settings: Settings,
        assistants: dict[str, Assistant],
        loop: AgentLoop,
        store: ConversationStore,
        trigger: TriggerEngine,
        window: GroupWindow,
        bindings: PersonaBindings,
        perms: PermissionGate,
        memory: MemoryService | None = None,
        archive: MessageArchive | None = None,
        summary: GroupSummaryService | None = None,
        social: SocialService | None = None,
        social_cards: SocialCards | None = None,
        shop: ShopService | None = None,
        charger: ToolCharger | None = None,
        duel: DuelService | None = None,
        game24: Game24Service | None = None,
        blackjack: BlackjackService | None = None,
        likes: LikeService | None = None,
        report: DailyReportService | None = None,
        linkparse: LinkParseService | None = None,
        imagegen: ImageGenService | None = None,
        groupadmin: GroupAdminService | None = None,
        weather: WeatherService | None = None,
        helper: HelpService | None = None,
        pig: PigService | None = None,
        superuser: SuperuserService | None = None,
        stats: StatsService | None = None,
        stickers: StickerService | None = None,
        websearch=None,
        music=None,
    ):
        self.settings = settings
        self.assistants = assistants
        self.loop = loop
        self.store = store
        self.trigger = trigger
        self.window = window
        self.bindings = bindings
        self.perms = perms
        self.perm_commands = PermCommands(perms)
        self.memory = memory
        self.archive = archive
        self.summary = summary
        self.social = social
        self.social_cards = social_cards
        self.shop = shop
        self.charger = charger
        self.duel = duel
        self.game24 = game24
        self.blackjack = blackjack
        self.likes = likes
        self.report = report
        self.linkparse = linkparse
        self.imagegen = imagegen
        self.groupadmin = groupadmin
        self.weather = weather
        self.helper = helper
        self.pig = pig
        self.superuser = superuser
        self.stats = stats
        self.stickers = stickers
        self.greeting = GreetingService()
        self.websearch = websearch
        self.music = music
        self.repeater = Repeater(settings.trigger.repeat)
        self.commands = self._build_commands()

    async def resolve_assistant(self, session: SessionKey) -> Assistant:
        if bound := await self.bindings.get(session):
            if assistant := self.assistants.get(bound):
                return assistant
        return self.assistants[self.settings.default_assistant]

    def is_superuser(self, incoming: IncomingMessage) -> bool:
        return incoming.user_id in self.settings.onebot.superusers

    async def handle(self, incoming: IncomingMessage, channel: Channel) -> None:
        is_group = incoming.session.chat_type == "group"
        bare_mention = is_group and incoming.at_me and not incoming.text and not incoming.images
        bare_wake = is_group and self.trigger.is_bare_wake(incoming.text)
        if not incoming.text and not incoming.images and not bare_mention:
            return
        group_id = incoming.session.chat_id if is_group else None
        # 被封禁的用户/群：完全无视（不回复、不参与复读，也不进入归档）
        if await self.perms.is_banned(incoming.user_id, group_id):
            return
        if is_group:
            state = await self.perms.group_state(incoming.session.chat_id)
            if not state.active:
                # 休眠中只响应权限指令（唤醒等）
                await self.perm_commands.handle(incoming, channel)
                return
        if await self.perm_commands.handle(incoming, channel):
            return
        # 超管设施（请求审批/广播/作弊器/列表/退群）：非超管内部直接放行
        if self.superuser is not None and await self.superuser.handle(
            incoming, channel
        ):
            return
        if self.groupadmin is not None and await self.groupadmin.handle_command(
            incoming, channel
        ):
            return
        # 决斗小游戏：入口与对局内指令（打/选/用等只在有对局时生效）
        if is_group and self.duel is not None and self.duel.wants(incoming):
            blocked = await self.perms.feature_blocked(
                incoming.session.chat_id, "决斗"
            )
            if await self.duel.handle(incoming, channel, blocked):
                await self._record_stat("command", "决斗", incoming)
                return
        # 24 点抢答：入口与对局内算式（算式只在有对局时才被识别）
        if is_group and self.game24 is not None and self.game24.wants(incoming):
            blocked = await self.perms.feature_blocked(
                incoming.session.chat_id, "24点"
            )
            if await self.game24.handle(incoming, channel, blocked):
                await self._record_stat("command", "24点", incoming)
                return
        # 21 点（blackjack）：入口与对局内拿牌/停牌
        if (
            is_group
            and self.blackjack is not None
            and self.blackjack.wants(incoming)
        ):
            blocked = await self.perms.feature_blocked(
                incoming.session.chat_id, "21点"
            )
            if await self.blackjack.handle(incoming, channel, blocked):
                await self._record_stat("command", "21点", incoming)
                return
        if await self.commands.dispatch(incoming, channel):
            if feature := feature_for_command(incoming.text.strip()):
                await self._record_stat("command", feature, incoming)
            return
        if bare_mention or bare_wake:
            if await self.perms.feature_blocked(
                incoming.session.chat_id, "闲聊"
            ):
                return
            assistant = await self.resolve_assistant(incoming.session)
            greeted = await self.greeting.greet(
                incoming, channel, assistant.greeting, self.stickers
            )
            if greeted:
                greeting_text = self.greeting.last_text(str(incoming.session))
                if greeting_text:
                    await self.window.push(
                        incoming.session,
                        WindowEntry(
                            user_id=incoming.self_id or "bot",
                            user_name=assistant.nickname,
                            text=greeting_text,
                        ),
                    )
                    await self._archive(
                        incoming.session,
                        incoming.self_id or "bot",
                        assistant.nickname,
                        greeting_text,
                    )
                await self._record_stat("chat", "问候", incoming)
            return
        if is_group and incoming.text and not bare_wake:
            await self.window.push(
                incoming.session,
                WindowEntry(
                    user_id=incoming.user_id,
                    user_name=incoming.user_name,
                    text=incoming.text,
                ),
            )
            await self._archive(incoming.session, incoming.user_id,
                                incoming.user_name, incoming.text)
        # 链接自动解析：群里普通消息（非 @）出现分享链接时回摘要卡；
        # @/唤醒场景留给 AI 经 parse_link 工具处理
        if (
            is_group
            and not incoming.at_me
            and self.linkparse is not None
            and not await self.perms.feature_blocked(
                incoming.session.chat_id, "解析"
            )
            and await self.linkparse.handle_message(incoming, channel)
        ):
            return
        # 复读机优先于 AI 触发；@消息不参与复读
        if (
            is_group
            and not incoming.at_me
            and not await self.perms.feature_blocked(
                incoming.session.chat_id, "复读"
            )
            and await self._handle_repeat(incoming, channel)
        ):
            return
        if is_group and await self.perms.feature_blocked(
            incoming.session.chat_id, "闲聊"
        ):
            return
        if not self.trigger.should_reply(incoming):
            return

        # OneBot 图片 URL 通常带 QQ 临时签名，外部模型服务未必能访问；
        # 在确认需要回复后由渠道本地化，避免无关群图片产生下载开销。
        await channel.prepare_incoming(incoming)

        assistant = await self.resolve_assistant(incoming.session)
        # 直接对话 = @ / 私聊 / 点名唤醒；只有概率插话走伪人语境（无工具上下文）
        direct = (
            incoming.at_me
            or not is_group
            or self.trigger.is_wake(incoming.text)
        )
        memory_block = ""
        if self.memory is not None and self.settings.memory.enable:
            memory_block = self.memory.render_block(
                await self.memory.recall(incoming)
            )
        social_block = ""
        if self.social is not None:
            social_block = await self.social.favor_block(
                incoming.user_id, incoming.user_name
            )
        # 表情包：可用时注入情绪标记说明（伪人插话同样生效）
        sticker_ok = (
            self.stickers is not None
            and self.stickers.enabled
            and (
                not is_group
                or not await self.perms.feature_blocked(
                    incoming.session.chat_id, "表情包"
                )
            )
        )
        sticker_block = self.stickers.prompt_block() if sticker_ok else ""

        history = await self.store.get(incoming.session)
        if direct:
            user_msg = build_user_message(incoming)
        else:
            # 伪人模式：以群窗口为语境接话
            user_msg = build_bym_user_message(
                incoming, await self.window.recent(incoming.session)
            )
        messages: list[ChatMessage] = [
            build_system_message(
                assistant, incoming, memory_block, social_block, sticker_block
            ),
            *history,
            user_msg,
        ]

        # 伪人模式不携带会话上下文：签到等上下文工具对路过接话不可见
        tool_ctx = (
            ToolContext(
                incoming=incoming,
                channel=channel,
                bot_name=assistant.nickname,
                disabled_tools=await self.perms.disabled_tools(group_id),
                charger=self.charger,
                stats=self.stats,
            )
            if direct
            else None
        )
        final: TurnComplete | None = None
        try:
            async for event in self.loop.run(assistant, messages, tool_ctx):
                if isinstance(event, TurnComplete):
                    final = event
        except Exception:
            logger.exception(f"agent 回复失败: {incoming.session}")
            if direct:
                # @/私聊必回场景不静默，给出可见的失败提示
                await channel.send_text(
                    incoming.session,
                    f"呜…{assistant.nickname}的脑子连不上了，过会再试试吧",
                )
            return
        reply = clean_reply(final.message.text()) if final else ""
        if is_skip(reply):
            # 主动不理会（辱骂/敏感内容/没在跟她说话）：不发消息、不发表情、
            # 也不写进历史——把这些内容留在上下文里只会污染后续对话
            logger.info(
                f"[{incoming.session}] 跳过回复 {incoming.user_name}: "
                f"{incoming.text[:50]}"
            )
            return
        # 表情标记无论是否发送都要剥离，不能泄漏到文本/历史里
        emotion = None
        if self.stickers is not None and reply:
            reply, emotion = self.stickers.extract(reply)
        if not reply:
            logger.warning(f"agent 无输出: {incoming.session}")
            return

        persisted_user = (
            persistable_user_message(incoming)
            if direct
            else ChatMessage(
                role="user",
                content=f"[{incoming.user_name}({incoming.user_id})]: {incoming.text}",
            )
        )
        await self.store.append(
            incoming.session,
            persisted_user,
            ChatMessage(role="assistant", content=reply),
        )
        if is_group:
            await self.window.push(
                incoming.session,
                WindowEntry(
                    user_id=incoming.self_id or "bot",
                    user_name=assistant.nickname,
                    text=reply,
                ),
            )
            await self._archive(
                incoming.session, incoming.self_id or "bot",
                assistant.nickname, reply,
            )
        self.trigger.mark_replied(incoming.session)
        await self._record_stat("chat", "闲聊", incoming)
        await self._send_reply(incoming, channel, assistant, reply)
        if emotion is not None and sticker_ok:
            assert self.stickers is not None
            await self.stickers.send(channel, incoming.session, emotion)
        self._maybe_extract_memory(incoming, history, persisted_user, reply)
        logger.info(
            f"[{incoming.session}] {incoming.user_name}: "
            f"{incoming.text[:50]} -> {reply[:100]}"
        )

    async def _record_stat(
        self, kind: str, name: str, incoming: IncomingMessage
    ) -> None:
        if self.stats is not None:
            await self.stats.record(kind, name, incoming)

    async def _handle_repeat(
        self, incoming: IncomingMessage, channel: Channel
    ) -> bool:
        """复读判定与执行。返回 True 表示本条消息已被复读机消费"""
        action = self.repeater.feed(
            str(incoming.session), content_key(incoming.text, incoming.image_keys)
        )
        if action is None:
            return False
        if action == "break":
            echo = self.repeater.break_text()
            await channel.send_text(incoming.session, echo)
        else:
            echo = incoming.text or "[图片]"
            if incoming.text:
                await channel.send_text(incoming.session, incoming.text)
            for url in incoming.images:
                try:
                    await channel.send_image(incoming.session, url)
                except NotImplementedError:
                    logger.warning(f"channel {channel.name} 不支持图片复读")
                    break
        await self.window.push(
            incoming.session,
            WindowEntry(
                user_id=incoming.self_id or "bot", user_name="bot", text=echo
            ),
        )
        await self._archive(
            incoming.session, incoming.self_id or "bot", "bot", echo
        )
        logger.info(f"[{incoming.session}] 复读({action}): {echo[:50]}")
        return True

    async def _archive(
        self, session: SessionKey, user_id: str, user_name: str, text: str
    ) -> None:
        if self.archive is None:
            return
        try:
            await self.archive.add(session.chat_id, user_id, user_name, text)
        except Exception:
            logger.exception("群消息归档失败")

    def _maybe_extract_memory(
        self,
        incoming: IncomingMessage,
        history: list[ChatMessage],
        user_msg: ChatMessage,
        reply: str,
    ) -> None:
        if self.memory is None or not self.settings.memory.enable:
            return
        if not self.memory.should_extract(incoming):
            return
        recent = [*history, user_msg, ChatMessage(role="assistant", content=reply)]
        task = asyncio.create_task(self.memory.extract_safely(incoming, recent))
        task.add_done_callback(lambda t: t.exception())

    async def _send_reply(
        self,
        incoming: IncomingMessage,
        channel: Channel,
        assistant: Assistant,
        reply: str,
    ) -> None:
        # 被点名时也不引用不 @，但同样按句分条发——一次糊一大段不像人说话
        for segment, delay in split_reply(reply, assistant.policy):
            if delay:
                await asyncio.sleep(delay)
            await channel.send_text(incoming.session, segment)

    # ---- 内置指令：注册表分发（各功能模块提供构建器） ---------------------

    def _build_commands(self) -> CommandRouter:
        """按 if 链时代的顺序装配指令注册表（长前缀先注册）。

        会话管理类指令（reset/记忆/人格）留在本类，其余分发逻辑都在
        各自模块的 build_xxx_commands 里。
        """
        available = {
            key
            for key, svc in {
                "social": self.social,
                "weather": self.weather,
                "imagegen": self.imagegen,
                "summary": self.summary,
                "report": self.report,
                "scheduler": self.summary,  # 调度器与总结同随 database 启用
                "websearch": self.websearch,
            }.items()
            if svc is not None
        }
        router = CommandRouter(self.perms)
        router.add(*build_help_commands(self.helper, available, self.perms))
        router.add(
            Command(exact("reset", lower=True), self._cmd_reset),
            Command(exact("清除记忆", "清空记忆"), self._cmd_clear_memory),
            Command(exact("人格列表"), self._cmd_list_personas),
        )
        router.add(
            *build_summary_commands(
                self.summary, self.perms, self.settings.summary.default_count
            )
        )
        if self.pig is not None:
            router.add(*build_pig_commands(self.pig))
        router.add(
            *build_social_commands(
                self.social, self.social_cards, self.resolve_assistant
            )
        )
        router.add(*build_shop_commands(self.shop, self.social_cards))
        router.add(*build_imagegen_commands(self.imagegen, self.perms))
        if self.weather is not None:
            router.add(build_weather_command(self.weather, self.perms))
        router.add(*build_report_commands(self.report, self.perms))
        if self.likes is not None:
            router.add(
                *build_like_commands(self.likes, self.resolve_assistant)
            )
        router.add(Command(prefix("切换人格"), self._cmd_switch_persona))
        return router

    # ---- 会话管理指令（依赖会话存储/人格绑定，留在本类） ------------------

    async def _cmd_reset(
        self, incoming: IncomingMessage, channel: Channel, rest: str
    ) -> bool:
        is_group = incoming.session.chat_type == "group"
        if is_group and not await self.perms.can_manage(incoming):
            if incoming.at_me:
                await channel.send_text(
                    incoming.session, "群聊会话只有管理者能重置哦"
                )
            return True
        await self.store.reset(incoming.session)
        assistant = await self.resolve_assistant(incoming.session)
        await channel.send_text(
            incoming.session, f"{assistant.nickname}忘记了之前的对话…"
        )
        return True

    async def _cmd_clear_memory(
        self, incoming: IncomingMessage, channel: Channel, rest: str
    ) -> bool:
        is_group = incoming.session.chat_type == "group"
        if self.memory is None or not self.settings.memory.enable:
            await channel.send_text(incoming.session, "长期记忆未启用")
            return True
        if is_group:
            # 群记忆是共有资产，仅超管可清（比普通管理指令更严格）
            if not self.perms.is_superuser(incoming.user_id):
                await channel.send_text(
                    incoming.session, "群里的记忆只有管理者能清除哦"
                )
                return True
            count = await self.memory.clear_scope(
                f"group:{incoming.session.chat_id}"
            )
        else:
            count = await self.memory.clear_scope(f"user:{incoming.user_id}")
        scope_name = "本群" if is_group else "关于你"
        await channel.send_text(
            incoming.session,
            (
                f"唔…{scope_name}的 {count} 条记忆都忘掉了…"
                if count
                else f"我还没有{scope_name}的记忆哦"
            ),
        )
        return True

    async def _cmd_list_personas(
        self, incoming: IncomingMessage, channel: Channel, rest: str
    ) -> bool:
        current = await self.resolve_assistant(incoming.session)
        lines = [
            f"{'* ' if name == current.name else '  '}{name}（{a.nickname}）"
            for name, a in self.assistants.items()
        ]
        await channel.send_text(
            incoming.session, "可用人格：\n" + "\n".join(lines)
        )
        return True

    async def _cmd_switch_persona(
        self, incoming: IncomingMessage, channel: Channel, name: str
    ) -> bool:
        if (
            incoming.session.chat_type == "group"
            and not await self.perms.can_manage(incoming)
        ):
            await channel.send_text(
                incoming.session, "群聊人格只有管理者能切换哦"
            )
            return True
        if name not in self.assistants:
            await channel.send_text(
                incoming.session,
                f"没有叫 {name} 的人格，发送「人格列表」查看",
            )
            return True
        await self.bindings.set(incoming.session, name)
        await channel.send_text(
            incoming.session,
            f"我现在是{self.assistants[name].nickname}了！",
        )
        return True
