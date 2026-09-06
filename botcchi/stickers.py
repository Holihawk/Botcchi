"""表情包系统：波奇按心情发表情包（QQ 表情形式，非大图）。

素材在 assets/stickers/<情绪>/ 下按目录归类，目录名即情绪标签，
加新表情 = 丢文件进目录，启动时扫描注册。

触发走「回复尾标记」而非工具：system prompt 告知可用情绪，模型在合适时
于回复末尾附 [表情:害羞]；发送前把标记剥离，按情绪随机挑一张以
sub_type=1（QQ 表情）发出。标记法在伪人插话（无工具上下文）场景同样有效。
防刷屏：每会话冷却 + 自动/显式独立概率闸；同情绪近期不重复（小 LRU）。
另有 send_sticker 工具供模型在直接对话中主动斗图。
"""

import random
import re
import time
from pathlib import Path

from loguru import logger

from .channels.base import Channel, SessionKey
from .config import StickerSettings
from .render import to_onebot_file
from .tools.registry import Tool, ToolContext, tool

ASSETS = Path(__file__).parent / "assets" / "stickers"
EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
RECENT_MAX = 3
"""同一情绪最近发过的几张不重复（不超过该情绪张数一半时生效）"""

_MARKER = re.compile(r"\s*[\[【［]\s*表情\s*[:：]\s*(?P<emo>[^\]】］]{1,12})\s*[\]】］]")


class StickerService:
    def __init__(
        self,
        settings: StickerSettings,
        assets: Path = ASSETS,
        rng: random.Random | None = None,
    ):
        self.settings = settings
        self.rng = rng or random.Random()
        self.stickers: dict[str, list[Path]] = {}
        """情绪 → 素材文件列表"""
        self._last_sent: dict[str, float] = {}
        """会话 → 上次发表情的时刻（冷却用）"""
        self._recent: dict[str, list[Path]] = {}
        if assets.is_dir():
            for d in sorted(assets.iterdir()):
                files = sorted(
                    f for f in d.iterdir()
                    if f.is_file() and f.suffix.lower() in EXTS
                ) if d.is_dir() else []
                if files:
                    self.stickers[d.name] = files
        total = sum(len(v) for v in self.stickers.values())
        if total:
            logger.info(
                f"表情包已加载: {len(self.stickers)} 类共 {total} 张"
            )

    @property
    def enabled(self) -> bool:
        return self.settings.enabled and bool(self.stickers)

    # ---- prompt 注入与标记提取 ------------------------------------------

    def prompt_block(self) -> str:
        if not self.enabled:
            return ""
        emotions = "、".join(self.stickers)
        return (
            "\n\n[表情包] 回复中有明确情绪时，在最末尾附上对应的情绪标记"
            "（格式：[表情:情绪]），系统会自行决定是否配图。可用情绪："
            f"{emotions}。日常的开心、疑惑、害羞、无语等情绪也可以标记，"
            "不必等到情绪非常强烈。至多标记一个，必须放在最后，别解释它。"
        )

    def extract(self, text: str) -> tuple[str, str | None]:
        """剥离文本中的全部表情标记，返回 (干净文本, 首个有效情绪)。"""
        emotion: str | None = None
        for m in _MARKER.finditer(text):
            emo = m.group("emo").strip()
            if emotion is None and emo in self.stickers:
                emotion = emo
        return _MARKER.sub("", text).strip(), emotion

    # ---- 挑选与发送 ------------------------------------------------------

    def pick(
        self,
        session: SessionKey,
        emotion: str,
        *,
        probability: float | None = None,
    ) -> Path | None:
        """冷却/概率闸后随机挑一张；不通过返回 None（不计冷却）"""
        files = self.stickers.get(emotion)
        if not files or not self.enabled:
            return None
        key = str(session)
        now = time.monotonic()
        last = self._last_sent.get(key)
        if last is not None and now - last < self.settings.cooldown_seconds:
            return None
        chance = self.settings.probability if probability is None else probability
        if self.rng.random() >= chance:
            return None
        recent = self._recent.setdefault(emotion, [])
        pool = [f for f in files if f not in recent] or files
        choice = self.rng.choice(pool)
        recent.append(choice)
        if len(recent) > min(RECENT_MAX, len(files) // 2):
            recent.pop(0)
        self._last_sent[key] = now
        return choice

    async def send(
        self,
        channel: Channel,
        session: SessionKey,
        emotion: str,
        *,
        probability: float | None = None,
    ) -> bool:
        """挑选并发送表情包；channel 不支持或发送失败静默放弃"""
        file = self.pick(session, emotion, probability=probability)
        if file is None:
            return False
        try:
            await channel.send_sticker(session, to_onebot_file(file.read_bytes()))
            return True
        except NotImplementedError:
            return False
        except Exception:
            logger.exception(f"表情包发送失败: {emotion}/{file.name}")
            return False

    async def send_auto(
        self, channel: Channel, session: SessionKey, emotion: str
    ) -> bool:
        """按自动回复概率发送模型标注的情绪表情"""
        return await self.send(
            channel,
            session,
            emotion,
            probability=self.settings.auto_probability,
        )

    async def send_random(
        self, channel: Channel, session: SessionKey, emotions: tuple[str, ...]
    ) -> bool:
        """随机发送指定情绪之一；供固定要求附带表情的系统回复使用"""
        if not self.enabled:
            return False
        available = [emotion for emotion in emotions if self.stickers.get(emotion)]
        if not available:
            return False
        emotion = self.rng.choice(available)
        files = self.stickers[emotion]
        file = self.rng.choice(files)
        try:
            await channel.send_sticker(session, to_onebot_file(file.read_bytes()))
            return True
        except NotImplementedError:
            return False
        except Exception:
            logger.exception(f"随机问候表情发送失败: {emotion}/{file.name}")
            return False


def build_sticker_tool(stickers: StickerService) -> Tool:
    @tool(
        name="send_sticker",
        description=(
            "往聊天里发一张表情包（QQ 表情形式）。emotion 为情绪标签，"
            "可选：" + "、".join(stickers.stickers) + "。"
            "用户想看表情包/斗图，或你想用表情回应时调用。"
            "有发送冷却，失败时不必重试。"
        ),
    )
    async def send_sticker(ctx: ToolContext, emotion: str) -> str:
        emotion = emotion.strip()
        if emotion not in stickers.stickers:
            return f"没有「{emotion}」这类表情，可选：" + "、".join(
                stickers.stickers
            )
        if await stickers.send(ctx.channel, ctx.incoming.session, emotion):
            return "表情包已发送"
        return "表情包没发出去（冷却中或渠道不支持），不用重试"

    return send_sticker
