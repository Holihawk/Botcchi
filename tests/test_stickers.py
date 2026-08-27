"""表情包系统：素材注册、标记提取、冷却/概率闸、端到端发送与功能开关。"""

import random

from conftest import text_chunks
from test_service import FakeChannel, make_settings

from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.config import StickerSettings
from botcchi.stickers import StickerService, build_sticker_tool


def make_service(**kw) -> StickerService:
    return StickerService(StickerSettings(**kw), rng=random.Random(7))


def test_registry_scan():
    svc = make_service()
    assert len(svc.stickers) == 16
    assert sum(len(v) for v in svc.stickers.values()) == 67
    assert "开心" in svc.stickers and "摆烂" in svc.stickers
    assert svc.enabled
    block = svc.prompt_block()
    assert "[表情:" in block and "开心" in block


def test_extract_markers():
    svc = make_service()
    assert svc.extract("好耶！[表情:开心]") == ("好耶！", "开心")
    # 全角括号/冒号、【】变体
    assert svc.extract("呜呜【表情：哭哭】") == ("呜呜", "哭哭")
    # 未知情绪：标记剥掉但不发
    assert svc.extract("嗯[表情:量子纠缠]") == ("嗯", None)
    # 多个标记只取第一个有效的，全部剥离
    text, emo = svc.extract("[表情:无语]中间[表情:开心]")
    assert text == "中间" and emo == "无语"
    assert svc.extract("没有标记") == ("没有标记", None)


def test_pick_cooldown_and_probability():
    session = SessionKey(channel="fake", chat_type="group", chat_id="777")
    svc = make_service(cooldown_seconds=3600)
    first = svc.pick(session, "开心")
    assert first is not None and first.parent.name == "开心"
    assert svc.pick(session, "开心") is None  # 冷却中
    other = SessionKey(channel="fake", chat_type="group", chat_id="888")
    assert svc.pick(other, "哭哭") is not None  # 冷却按会话隔离

    svc2 = make_service(probability=0.0)
    assert svc2.pick(session, "开心") is None  # 概率闸不放行
    assert not make_service(enabled=False).enabled


class StickerChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.stickers: list[str] = []

    async def send_sticker(self, session, file):
        self.stickers.append(file)


def msg(text: str, chat_id: str = "10001") -> IncomingMessage:
    return IncomingMessage(
        session=SessionKey(channel="fake", chat_type="private", chat_id=chat_id),
        user_id=chat_id,
        user_name="hhk",
        text=text,
        at_me=True,
    )


async def test_end_to_end_marker_send(mock_openai, tmp_path):
    from botcchi.app import build_context

    ctx = build_context(make_settings(tmp_path), client=mock_openai.client())
    channel = StickerChannel()

    mock_openai.enqueue(text_chunks("好耶！[表情:开心]"))
    await ctx.service.handle(msg("波奇开心吗"), channel)
    assert channel.sent[-1][1] == "好耶！"  # 标记已剥离
    assert len(channel.stickers) == 1
    assert channel.stickers[0].startswith("base64://")
    # 历史里存的是干净文本
    history = await ctx.service.store.get(msg("").session)
    assert "[表情" not in history[-1].content

    # 冷却中：第二次标记不再出图
    mock_openai.enqueue(text_chunks("呜呜[表情:哭哭]"))
    await ctx.service.handle(msg("再来"), channel)
    assert channel.sent[-1][1] == "呜呜"
    assert len(channel.stickers) == 1
    # system prompt 已注入表情说明
    body = mock_openai.requests[-1]["messages"][0]["content"]
    assert "[表情包]" in body
    await ctx.shutdown()


async def test_feature_blocked_strips_but_not_send(mock_openai, tmp_path):
    from botcchi.app import build_context

    settings = make_settings(tmp_path)
    settings.database.url = f"sqlite+aiosqlite:///{tmp_path}/app.db"
    settings.onebot.superusers = ["900000"]
    ctx = build_context(settings, client=mock_openai.client())
    await ctx.db.init()
    channel = StickerChannel()

    group = IncomingMessage(
        session=SessionKey(channel="fake", chat_type="group", chat_id="777"),
        user_id="900000",
        user_name="su",
        text="关闭功能 表情包",
    )
    await ctx.service.handle(group, channel)

    mock_openai.enqueue(text_chunks("嘿嘿[表情:开心]"))
    ask = IncomingMessage(
        session=group.session, user_id="10001", user_name="hhk",
        text="波奇在吗", at_me=True,
    )
    await ctx.service.handle(ask, channel)
    assert channel.sent[-1][1] == "嘿嘿"  # 标记仍被剥离
    assert channel.stickers == []  # 但不发表情
    # 关闭后不注入表情说明
    body = mock_openai.requests[-1]["messages"][0]["content"]
    assert "[表情包]" not in body
    await ctx.shutdown()


async def test_send_sticker_tool():
    from botcchi.tools.registry import ToolContext

    svc = make_service()
    t = build_sticker_tool(svc)
    channel = StickerChannel()
    ctx = ToolContext(incoming=msg(""), channel=channel)
    out = await t.execute('{"emotion": "开心"}', ctx)
    assert out == "表情包已发送" and len(channel.stickers) == 1
    out = await t.execute('{"emotion": "不存在"}', ctx)
    assert "没有「不存在」" in out
    out = await t.execute('{"emotion": "开心"}', ctx)  # 冷却中
    assert "没发出去" in out
