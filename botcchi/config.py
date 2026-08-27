"""全局配置：从 YAML 加载，环境变量 BOTCCHI_CONFIG 可指定路径"""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class ModelDetail(BaseModel):
    name: str
    temperature: float | None = None
    max_tokens: int | None = None


class ProviderSettings(BaseModel):
    name: str
    api_type: Literal["openai"] = "openai"
    api_base: str
    api_keys: list[str]
    models: list[ModelDetail] = Field(default_factory=list)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    proxy: str | bool | None = None
    """代理策略：省略/true = 跟随 http(s)_proxy 环境变量；
    false = 强制直连（国内端点建议，免受本地代理状态影响）；
    字符串 = 指定代理地址（如 http://127.0.0.1:7897）"""


class ServerSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8300


class OneBotSettings(BaseModel):
    path: str = "/onebot/v11/ws"
    access_token: str | None = None
    superusers: list[str] = Field(default_factory=list)


class RepeatSettings(BaseModel):
    """复读机：连续相同消息达到次数后跟着复读"""

    enabled: bool = True
    trigger_count: int = 3
    probability: float = 0.7
    """达到次数后实际跟读的概率"""
    break_probability: float = 0.2
    """跟读时改为打断的概率（打断施法彩蛋）"""
    break_texts: list[str] = Field(default_factory=lambda: ["打断施法！"])


class TriggerSettings(BaseModel):
    private_chat: bool = True
    group_reply_rate: float = 0.0
    """群内非 @ 消息的随机回复概率（伪人模式），0 关闭"""
    cooldown_seconds: float = 0.0
    wake_words: list[str] = Field(default_factory=list)
    """消息包含任一唤醒词时必定回复（如机器人昵称）"""
    group_window_size: int = 30
    """伪人模式感知的群聊上下文条数"""
    repeat: RepeatSettings = Field(default_factory=RepeatSettings)


class DatabaseSettings(BaseModel):
    url: str | None = None
    """如 postgresql+asyncpg://botcchi:botcchi@localhost:5432/botcchi；
    留空则使用进程内存储（重启丢失，适合调试）"""


class RedisSettings(BaseModel):
    url: str | None = None
    """如 redis://localhost:6379/0；留空则群窗口使用进程内实现"""


class MemorySettings(BaseModel):
    enable: bool = True
    model: str | None = None
    """记忆提取用的模型引用；留空复用默认 Assistant 的模型"""
    extract_every: int = 8
    """每个会话每积累多少轮对话触发一次提取"""
    max_inject: int = 10
    """注入 system prompt 的记忆条数上限"""
    max_per_scope: int = 200
    """单个用户/群的记忆容量，超出裁剪最旧的"""


class MCPServerSettings(BaseModel):
    """一个外部 MCP 工具服务器（stdio 子进程或 streamable HTTP）"""

    name: str
    transport: Literal["stdio", "http"] = "stdio"
    enabled: bool = True
    command: str | None = None
    """stdio：启动命令，如 uvx / npx / python"""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    """http：MCP 服务地址，如 http://localhost:8000/mcp"""
    headers: dict[str, str] = Field(default_factory=dict)


class RenderSettings(BaseModel):
    """SVG → PNG 渲染（签到卡片等出图功能共用）。"""

    zoom: float = 2.0
    """SVG 逻辑像素到输出像素的放大倍率（2 = 高清）"""
    font_family: str = "Noto Sans CJK SC"
    """默认字体（需系统已安装中文字体；Docker 镜像需装 fonts-noto-cjk）"""
    font_dirs: list[str] = Field(default_factory=list)
    """额外字体目录（系统字体之外自带字体文件时使用）"""


class SocialSettings(BaseModel):
    """签到/好感度/金币（需要 database.url）。"""

    enable: bool = True
    double_probability: float = 0.09
    """签到好感度双倍的触发概率"""
    streak_bonus_step: int = 5
    """连续签到每天的金币加成步长"""
    streak_bonus_cap: int = 7
    """连签加成封顶天数（加成上限 = step * cap）"""
    initial_coins: int = 2000
    """新用户建档时的初始金币"""
    tool_costs: dict[str, int] = {}
    """LLM 工具按次收费（金币），如 {"draw_image": 100}；超管免费"""


class WeatherSettings(BaseModel):
    """和风天气（付费版专属 API host，JWT 或 api_key 认证二选一）"""

    api_host: str = ""
    """控制台分配的专属主机，如 https://xxxx.re.qweatherapi.com；留空关闭天气"""
    api_key: str = ""
    """X-QW-Api-Key 认证；与 JWT 二选一，JWT 优先"""
    jwt_sub: str = ""
    """项目 ID"""
    jwt_kid: str = ""
    """凭据 ID"""
    jwt_private_key: str = ""
    """Ed25519 私钥 PEM（支持 \\n 转义的单行形式）"""

    @property
    def enabled(self) -> bool:
        return bool(
            self.api_host
            and (
                self.api_key
                or (self.jwt_sub and self.jwt_kid and self.jwt_private_key)
            )
        )


class ImageGenSettings(BaseModel):
    """GPT 绘图（OpenAI images API 兼容端点）。

    与对话 providers 相互独立：绘图中转的 URL 和 key 单独配置。
    """

    api_base: str = ""
    """如 http://new.xem8k5.top:3000/v1；留空则绘图功能关闭"""
    api_key: str = ""
    model: str = "gpt-image-2"
    output_format: Literal["png", "jpeg", "webp"] = "jpeg"
    output_compression: int = Field(default=85, ge=0, le=100)
    moderation: Literal["auto", "low"] = "auto"
    timeout: float = 180.0
    max_retries: int = Field(default=2, ge=0, le=5)
    max_concurrency: int = Field(default=2, ge=1, le=10)
    cooldown_seconds: int = 240
    """同一用户两次绘图的最小间隔（超管不受限）"""
    max_prompt_length: int = 2000
    max_input_images: int = Field(default=4, ge=1, le=16)
    proxy: str | bool | None = None
    """代理策略，同 provider：省略跟随环境变量 / false 直连 / 字符串指定"""


class LinkParseSettings(BaseModel):
    """群聊链接自动解析与媒体转发。"""

    send_media: bool = True
    """解析后直接把视频/音频/图集发进群（关闭则只发摘要卡）"""
    max_video_seconds: int = 480
    """超过该时长的视频不搬运，只发摘要卡（0 = 不限制）"""
    max_media_mb: int = 50
    """单个媒体文件的大小上限（下载中超限即放弃）"""
    max_images: int = 9
    """图集最多转发的图片数"""
    max_content_images: int = 60
    """图文正文（合并转发）里最多搬运的图片数，比纯图集放宽以尽量保内容完整"""


class PermsSettings(BaseModel):
    """数值权限等级体系。超管（onebot.superusers）恒为最高等级"""

    owner_level: int = 8
    """群主的隐式等级"""
    admin_level: int = 5
    """群管理员的隐式等级"""
    manage_level: int = 5
    """使用群管理指令（封禁/功能开关/定时设置等）所需等级"""


class SummarySettings(BaseModel):
    """群聊总结（需要 database.url，消息归档为其数据源）。"""

    model: str | None = None
    """总结用的模型引用；留空复用默认 Assistant 的模型"""
    default_count: int = 200
    max_count: int = 1000
    cooldown_seconds: float = 300.0
    """同一群两次手动总结的最小间隔"""


class StickerSettings(BaseModel):
    """表情包：波奇按心情发 QQ 表情（assets/stickers/<情绪>/ 目录注册）"""

    enabled: bool = True
    cooldown_seconds: float = 45.0
    """同一会话两次表情包的最小间隔（秒）"""
    probability: float = 1.0
    """模型给出表情标记后实际发送的概率（0~1，调低可更克制）"""


class WebSearchSettings(BaseModel):
    """联网搜索与网页阅读（Tavily API）。未配置 api_key 则整体关闭"""

    api_key: str | None = None
    """Tavily API key（tvly-…），https://tavily.com 免费额度即可用"""
    max_results: int = 5
    """web_search 单次返回的结果条数上限"""


class LogSettings(BaseModel):
    """日志输出：控制台常开；配置 dir 后额外按天轮转落盘"""

    level: str = "INFO"
    dir: str | None = "data/logs"
    """日志文件目录；留空则只输出到控制台，不落盘。
    落盘时分两份：全量 {date}.log（按 level）+ error_{date}.log（仅 ERROR 及以上）"""
    retention_days: int = 15


class Settings(BaseModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    onebot: OneBotSettings = Field(default_factory=OneBotSettings)
    trigger: TriggerSettings = Field(default_factory=TriggerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    summary: SummarySettings = Field(default_factory=SummarySettings)
    perms: PermsSettings = Field(default_factory=PermsSettings)
    linkparse: LinkParseSettings = Field(default_factory=LinkParseSettings)
    imagegen: ImageGenSettings = Field(default_factory=ImageGenSettings)
    weather: WeatherSettings = Field(default_factory=WeatherSettings)
    social: SocialSettings = Field(default_factory=SocialSettings)
    stickers: StickerSettings = Field(default_factory=StickerSettings)
    websearch: WebSearchSettings = Field(default_factory=WebSearchSettings)
    render: RenderSettings = Field(default_factory=RenderSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    mcp_servers: list[MCPServerSettings] = Field(default_factory=list)
    providers: list[ProviderSettings] = Field(default_factory=list)
    providers_file: Path = Path("data/providers.yaml")
    """WebUI 管理的 provider 存储文件；存在时优先于 config.yaml 的 providers"""
    default_assistant: str = "bocchi"
    assistants_dir: Path = Path("data/assistants")
    history_max_messages: int = 40
    """单会话短期历史保留的消息条数上限"""


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path or os.environ.get("BOTCCHI_CONFIG", "config.yaml"))
    if not path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {path}（可从 config.example.yaml 复制）"
        )
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    settings = Settings.model_validate(data)
    # WebUI 管理的 providers 文件存在时整体接管（config.yaml 仅作首次引导）
    if settings.providers_file.exists():
        with settings.providers_file.open(encoding="utf-8") as f:
            stored = yaml.safe_load(f) or {}
        settings.providers = [
            ProviderSettings.model_validate(p) for p in stored.get("providers", [])
        ]
    return settings


def save_providers(path: Path, providers: list[ProviderSettings]) -> None:
    """把 provider 列表写入 WebUI 管理的存储文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            {"providers": [p.model_dump(exclude_none=True) for p in providers]},
            f,
            allow_unicode=True,
            sort_keys=False,
        )
