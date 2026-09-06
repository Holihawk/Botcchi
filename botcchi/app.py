"""应用组装：配置 → 组件 → FastAPI 实例。

database.url / redis.url 未配置时自动退化为进程内实现，便于零依赖调试。
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from . import __version__
from .agent.assistant import load_assistants
from .agent.loop import AgentLoop
from .campus_map import CampusMapService, build_campus_map_tool
from .channels.base import Channel
from .channels.onebot.adapter import OneBotAdapter
from .config import ProviderSettings, Settings
from .db.archive import MessageArchive
from .db.engine import Database
from .db.store import (
    DBConversationStore,
    InMemoryPersonaBindingStore,
    PersonaBindingStore,
)
from .games import BlackjackService, DuelService, Game24Service
from .groupadmin import GreetingStore, GroupAdminService
from .help import HelpService
from .imagegen import ImageGenService, build_imagegen_tool
from .linkparse import LinkParseService, build_parse_tool
from .memory.group_window import (
    GroupWindow,
    InMemoryGroupWindow,
    RedisGroupWindow,
)
from .memory.long_term import MemoryService, build_memory_tools
from .memory.short_term import InMemoryConversationStore
from .music import MusicService, build_music_tool, build_poke_tool
from .perms import PermissionGate, PermStore
from .pig import PigService, build_pig_tools
from .providers.registry import ProviderRegistry
from .reminders import (
    ReminderService,
    build_reminder_tools,
    register_reminder_job,
)
from .render import RenderCache, SvgRenderer
from .report import DailyReportService, ReportSchedules, register_report_job
from .report.service import build_report_schedule_tool, build_report_tool
from .scheduler import JobStore, Scheduler
from .service import ChatService
from .social.card import SocialCards
from .social.likes import LikeService
from .social.service import SocialService
from .social.shop import ShopService, ToolCharger, build_shop_tools, refund_escrows
from .social.tools import build_like_tool, register_social_tools
from .stats import StatsService
from .stickers import StickerService, build_sticker_tool
from .summary import (
    GroupSummaryService,
    SummarySchedules,
    build_summarize_tool,
    build_summary_schedule_tool,
    register_summary_job,
)
from .superuser import SuperuserService
from .tools.builtin import register_builtin
from .tools.mcp import MCPManager
from .tools.registry import ToolRegistry
from .trigger.engine import TriggerEngine
from .weather import WeatherService, build_weather_tool
from .websearch import WebSearchService, build_search_tools


@dataclass
class AppContext:
    settings: Settings
    service: ChatService
    providers: ProviderRegistry
    db: Database | None
    window: GroupWindow
    scheduler: Scheduler | None = None
    jobs: JobStore | None = None
    perms: PermissionGate | None = None
    perms_store: PermStore | None = None
    mcp: MCPManager | None = None
    http_client: httpx.AsyncClient | None = None
    channel: Channel | None = None
    """主 channel（OneBot 适配器），供定时任务等主动发送使用"""
    _retired: list[ProviderRegistry] = field(default_factory=list)
    _stop_scheduler: asyncio.Event = field(default_factory=asyncio.Event)
    _scheduler_task: asyncio.Task | None = None
    _requests_task: asyncio.Task | None = None

    async def startup(self) -> None:
        if self.db is not None:
            await self.db.init()
            if self.service.social is not None:
                # 崩溃/重启丢掉的对局：把托管中的游戏券退还给玩家
                users, tickets = await refund_escrows(self.db)
                if tickets:
                    logger.warning(
                        f"检测到上次未结束的对局，已向 {users} 名玩家"
                        f"退还共 {tickets} 张游戏券"
                    )
        if self.mcp is not None:
            await self.mcp.start(self.service.loop.tools)
        if self.scheduler is not None and self.channel is not None:
            self._scheduler_task = asyncio.create_task(
                self.scheduler.run(self.channel, self._stop_scheduler)
            )
        if self.service.superuser is not None and self.channel is not None:
            self._requests_task = asyncio.create_task(
                self.service.superuser.watch_requests(
                    self.channel, self._stop_scheduler
                )
            )

    def reload_providers(self, providers: list[ProviderSettings]) -> None:
        """用新的 provider 列表热替换注册表并持久化。

        旧注册表进入退役列表、关停时统一关闭，避免打断进行中的流式请求。
        """
        from .config import save_providers

        self._retired.append(self.providers)
        self.settings.providers = providers
        registry = ProviderRegistry.from_settings(providers, client=self.http_client)
        self.providers = registry
        self.service.loop.providers = registry
        if self.service.memory is not None:
            self.service.memory.providers = registry
        save_providers(self.settings.providers_file, providers)

    async def shutdown(self) -> None:
        self._stop_scheduler.set()
        if self._scheduler_task is not None:
            await self._scheduler_task
        if self._requests_task is not None:
            await self._requests_task
        if self.service.linkparse is not None:
            await self.service.linkparse.aclose()
        if self.service.imagegen is not None:
            await self.service.imagegen.aclose()
        if self.service.weather is not None:
            await self.service.weather.aclose()
        if self.service.duel is not None:
            await self.service.duel.aclose()
        if self.service.game24 is not None:
            await self.service.game24.aclose()
        if self.service.blackjack is not None:
            await self.service.blackjack.aclose()
        if self.service.pig is not None:
            await self.service.pig.aclose()
        if self.service.websearch is not None:
            await self.service.websearch.aclose()
        if self.service.music is not None:
            await self.service.music.aclose()
        if self.mcp is not None:
            await self.mcp.aclose()
        await self.providers.aclose()
        for retired in self._retired:
            await retired.aclose()
        if self.db is not None:
            await self.db.aclose()
        if isinstance(self.window, RedisGroupWindow):
            await self.window.aclose()


def build_context(
    settings: Settings, client: httpx.AsyncClient | None = None
) -> AppContext:
    assistants = load_assistants(settings.assistants_dir)
    if settings.default_assistant not in assistants:
        raise RuntimeError(
            f"默认 Assistant '{settings.default_assistant}' 不存在，"
            f"已加载: {list(assistants) or '（无）'}"
        )
    providers = ProviderRegistry.from_settings(settings.providers, client=client)
    tools = ToolRegistry()
    register_builtin(tools)

    db = Database(settings.database.url) if settings.database.url else None
    if db is not None:
        store = DBConversationStore(db, settings.history_max_messages)
        bindings = PersonaBindingStore(db)
    else:
        store = InMemoryConversationStore(settings.history_max_messages)
        bindings = InMemoryPersonaBindingStore()
        logger.warning("未配置 database.url，会话与人格绑定仅保存在内存中")

    if settings.redis.url:
        window: GroupWindow = RedisGroupWindow(
            settings.redis.url, settings.trigger.group_window_size
        )
    else:
        window = InMemoryGroupWindow(settings.trigger.group_window_size)

    default_model = assistants[settings.default_assistant].model
    memory = None
    if settings.memory.enable and db is not None:
        memory = MemoryService(settings.memory, db, providers, default_model)
        for t in build_memory_tools(memory):
            tools.register(t)
    elif settings.memory.enable:
        logger.warning("长期记忆需要配置 database.url，已自动禁用")

    # 表情包：素材目录有货才注册工具（标记法不依赖工具，见 stickers.py）
    stickers = StickerService(settings.stickers)
    if stickers.stickers:
        tools.register(build_sticker_tool(stickers))

    # 联网搜索（Tavily）：未配置 api_key 则整体关闭
    websearch = None
    if settings.websearch.api_key:
        websearch = WebSearchService(settings.websearch)
        for t in build_search_tools(websearch):
            tools.register(t)

    # 网易云点歌与戳一戳：无外部依赖，恒注册
    music = MusicService()
    tools.register(build_music_tool(music))
    tools.register(build_poke_tool())

    # 南邮校园地图：静态资源随包分发，无外部依赖
    campus_maps = CampusMapService()
    tools.register(build_campus_map_tool(campus_maps))

    perms_store = PermStore(db) if db is not None else None
    perms = PermissionGate(
        settings.perms, settings.onebot.superusers, perms_store
    )
    if perms_store is None:
        logger.warning("未配置 database.url，封禁/授权/群功能开关不可用")

    archive = None
    summary = None
    jobs = None
    scheduler = None
    if db is not None:
        archive = MessageArchive(db)
        jobs = JobStore(db)
        scheduler = Scheduler(jobs)
        summary = GroupSummaryService(
            settings.summary,
            archive,
            SummarySchedules(jobs),
            providers,
            default_model,
        )
        register_summary_job(scheduler, summary)
        tools.register(build_summarize_tool(summary))
        tools.register(build_summary_schedule_tool(summary, perms))
        reminders = ReminderService(jobs)
        for t in build_reminder_tools(reminders):
            tools.register(t)

    # 内容稳定的卡片走出图缓存，各功能一个子目录，淘汰互不干扰
    cards = RenderCache("data/cache/cards")
    # 帮助菜单按「可用服务 + 本群关闭的功能」组合成图，同组合的群共用一张
    helper = HelpService(SvgRenderer(settings.render), cache=cards.scope("help", 48))
    pig = PigService(SvgRenderer(settings.render), cache=cards.scope("pig", 128))
    for t in build_pig_tools(pig):
        tools.register(t)

    social = None
    social_cards = None
    shop = None
    charger = None
    duel = None
    game24 = None
    blackjack = None
    if settings.social.enable and db is not None:
        social = SocialService(settings.social, db)
        social_cards = SocialCards(
            SvgRenderer(settings.render), cache=cards.scope("social", 96)
        )
        register_social_tools(tools, social, social_cards)
        shop = ShopService(db, social)
        for t in build_shop_tools(shop):
            tools.register(t)
        duel = DuelService(db, social)
        game24 = Game24Service(db, social)
        blackjack = BlackjackService(db, social)
        if settings.social.tool_costs:
            charger = ToolCharger(
                social, settings.social.tool_costs, perms.is_superuser
            )
    elif settings.social.enable:
        logger.warning("签到/好感度需要配置 database.url，已自动禁用")

    # 点赞不依赖数据库；有 social 时回赞附带金币/好感度奖励
    likes = LikeService(social=social, archive=archive)
    tools.register(build_like_tool(likes))

    # 孤独日报：出图不依赖数据库；定时推送需要调度器
    report = DailyReportService(
        SvgRenderer(settings.render),
        schedules=ReportSchedules(jobs) if jobs is not None else None,
    )
    tools.register(build_report_tool(report))
    if scheduler is not None:
        register_report_job(scheduler, report)
        tools.register(build_report_schedule_tool(report, perms))

    # 链接解析：不依赖数据库；client 由服务自持，关停时统一关闭
    linkparse = LinkParseService(settings=settings.linkparse)
    tools.register(build_parse_tool(linkparse))

    # 群管理：迎新事件 + 文案管理 + 撤回；文案持久化需要数据库
    groupadmin = GroupAdminService(
        perms, GreetingStore(db) if db is not None else None
    )

    # 和风天气：付费版专属 host + JWT/apikey；未配置则整体关闭
    weather = None
    if settings.weather.enabled:
        weather = WeatherService(settings.weather, SvgRenderer(settings.render))
        tools.register(build_weather_tool(weather))

    # GPT 绘图：URL/key 独立于对话 providers，未配置则整体关闭
    imagegen = None
    if settings.imagegen.api_base and settings.imagegen.api_key:
        imagegen = ImageGenService(settings.imagegen)
        tools.register(build_imagegen_tool(imagegen, perms))

    superuser = SuperuserService(perms, db=db, social=social)
    stats = StatsService(db) if db is not None else None

    service = ChatService(
        settings=settings,
        assistants=assistants,
        loop=AgentLoop(providers, tools),
        store=store,
        trigger=TriggerEngine(settings.trigger),
        window=window,
        bindings=bindings,
        perms=perms,
        memory=memory,
        archive=archive,
        summary=summary,
        social=social,
        social_cards=social_cards,
        shop=shop,
        charger=charger,
        duel=duel,
        game24=game24,
        blackjack=blackjack,
        likes=likes,
        report=report,
        linkparse=linkparse,
        imagegen=imagegen,
        groupadmin=groupadmin,
        weather=weather,
        helper=helper,
        pig=pig,
        superuser=superuser,
        stats=stats,
        stickers=stickers,
        websearch=websearch,
        music=music,
    )
    if scheduler is not None:
        # 提醒执行器需要 ChatService（人格化文案生成），在其构造后注册
        register_reminder_job(scheduler, service)
    return AppContext(
        settings=settings,
        service=service,
        providers=providers,
        db=db,
        window=window,
        scheduler=scheduler,
        jobs=jobs,
        perms=perms,
        perms_store=perms_store,
        mcp=MCPManager(settings.mcp_servers) if settings.mcp_servers else None,
        http_client=client,
    )


def create_app(settings: Settings, client: httpx.AsyncClient | None = None) -> FastAPI:
    ctx = build_context(settings, client=client)

    async def on_profile_like(user_id: str, nickname: str, times: int) -> None:
        if ctx.service.likes is not None:
            await ctx.service.likes.on_profile_like(
                adapter, user_id, nickname, times
            )

    async def on_member_change(
        group_id: str, user_id: str, nickname: str, change: str,
        operator_name: str,
    ) -> None:
        if ctx.service.groupadmin is not None:
            await ctx.service.groupadmin.on_member_change(
                adapter, group_id, user_id, nickname, change, operator_name
            )

    async def on_request(req) -> None:
        if ctx.service.superuser is not None:
            await ctx.service.superuser.on_request(adapter, req)

    adapter = OneBotAdapter(
        settings.onebot,
        on_message=lambda incoming: ctx.service.handle(incoming, adapter),
        on_profile_like=on_profile_like,
        on_member_change=on_member_change,
        on_request=on_request,
    )
    ctx.channel = adapter

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await ctx.startup()
        logger.info(
            f"Botcchi v{__version__} 启动，"
            f"assistants={list(ctx.service.assistants)}，"
            f"持久化={'PostgreSQL' if ctx.db else '内存'}，"
            f"OneBot WS 路径={settings.onebot.path}"
        )
        yield
        await ctx.shutdown()

    app = FastAPI(title="Botcchi", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(adapter.router)
    app.state.ctx = ctx

    from .api import build_chat_router, build_management_router

    app.include_router(build_management_router(ctx))
    app.include_router(build_chat_router(ctx))

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    _mount_webui(app)
    return app


def _mount_webui(app: FastAPI) -> None:
    """托管前端构建产物（web/dist）；SPA 路由回退到 index.html"""
    dist = Path(__file__).resolve().parent.parent / "web" / "dist"
    index = dist / "index.html"
    if not index.exists():
        logger.info("web/dist 不存在，跳过 WebUI 托管（先 cd web && npm run build）")
        return
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        file = dist / path
        if path and file.is_file():
            return FileResponse(file)
        return FileResponse(index)
