"""ChatService 端到端 + OneBot 事件解析 + 回复切分。"""

from pathlib import Path

from conftest import text_chunks

from botcchi.agent.assistant import OutputPolicy
from botcchi.app import build_context
from botcchi.channels.base import Channel, IncomingMessage, SessionKey
from botcchi.channels.onebot.adapter import OneBotAdapter
from botcchi.config import (
    OneBotSettings,
    ProviderSettings,
    Settings,
    TriggerSettings,
)
from botcchi.service import split_reply


class FakeChannel(Channel):
    name = "fake"

    def __init__(self):
        self.sent: list[tuple[SessionKey, str, str | None]] = []
        self.ats: list[str | None] = []
        """与 sent 一一对应的 at_user"""
        self.stickers: list[tuple[SessionKey, str]] = []

    async def send_text(self, session, text, *, reply_to=None, at_user=None):
        self.sent.append((session, text, reply_to))
        self.ats.append(at_user)

    async def send_sticker(self, session, file):
        self.stickers.append((session, file))


def make_settings(tmp_path: Path) -> Settings:
    assistant_yaml = tmp_path / "test.yaml"
    assistant_yaml.write_text(
        "name: test\n"
        "display_name: 波奇\n"
        "model: mock/m\n"
        "prompt: 你是波奇\n"
        "policy:\n  split: false\n",
        encoding="utf-8",
    )
    return Settings(
        providers=[
            ProviderSettings(name="mock", api_base="http://mock/v1", api_keys=["k"])
        ],
        default_assistant="test",
        assistants_dir=tmp_path,
        trigger=TriggerSettings(private_chat=True, group_reply_rate=0.0),
    )


async def test_private_chat_end_to_end(mock_openai, tmp_path):
    mock_openai.enqueue(text_chunks("在的在的！"))
    service = build_context(make_settings(tmp_path), client=mock_openai.client()).service
    channel = FakeChannel()

    session = SessionKey(channel="fake", chat_type="private", chat_id="10001")
    await service.handle(
        IncomingMessage(
            session=session, user_id="10001", user_name="hhk", text="在吗", at_me=True
        ),
        channel,
    )

    assert channel.sent == [(session, "在的在的！", None)]
    # 历史已写入：user + assistant
    history = await service.store.get(session)
    assert [m.role for m in history] == ["user", "assistant"]


async def test_group_not_at_me_is_ignored(mock_openai, tmp_path):
    service = build_context(make_settings(tmp_path), client=mock_openai.client()).service
    channel = FakeChannel()

    await service.handle(
        IncomingMessage(
            session=SessionKey(channel="fake", chat_type="group", chat_id="777"),
            user_id="10001",
            user_name="hhk",
            text="随便聊聊",
            at_me=False,
        ),
        channel,
    )
    assert channel.sent == []
    assert mock_openai.requests == []


async def test_bare_group_wake_uses_fast_greeting(mock_openai, tmp_path):
    settings = make_settings(tmp_path)
    settings.trigger.wake_words = ["波奇"]
    service = build_context(settings, client=mock_openai.client()).service
    channel = FakeChannel()
    session = SessionKey(channel="fake", chat_type="group", chat_id="777")

    await service.handle(
        IncomingMessage(
            session=session,
            user_id="10001",
            user_name="hhk",
            text="波奇！",
        ),
        channel,
    )

    assert len(channel.sent) == 1
    assert len(channel.stickers) == 1
    assert mock_openai.requests == []
    assert await service.store.get(session) == []


async def test_empty_group_mention_uses_fast_greeting(mock_openai, tmp_path):
    service = build_context(make_settings(tmp_path), client=mock_openai.client()).service
    channel = FakeChannel()
    session = SessionKey(channel="fake", chat_type="group", chat_id="777")

    await service.handle(
        IncomingMessage(
            session=session,
            user_id="10001",
            user_name="hhk",
            text="",
            at_me=True,
        ),
        channel,
    )

    assert len(channel.sent) == 1
    assert len(channel.stickers) == 1
    assert mock_openai.requests == []


async def test_reset_command(mock_openai, tmp_path):
    service = build_context(make_settings(tmp_path), client=mock_openai.client()).service
    channel = FakeChannel()
    session = SessionKey(channel="fake", chat_type="private", chat_id="10001")

    await service.handle(
        IncomingMessage(
            session=session, user_id="10001", user_name="hhk", text="reset", at_me=True
        ),
        channel,
    )
    assert "忘记" in channel.sent[0][1]
    assert mock_openai.requests == []  # 指令不经 LLM


def test_split_reply_respects_max_segments():
    policy = OutputPolicy(split=True, max_segments=3, delay_per_char=0.1, max_delay=1.0)
    segments = split_reply("第一句。第二句？第三句！第四句。", policy)
    assert len(segments) == 3
    assert segments[0][0] == "第一句"
    assert "第三句" in segments[2][0] and "第四句" in segments[2][0]
    assert all(delay <= 1.0 for _, delay in segments)


def test_split_reply_breaks_overlong_sentence():
    """超过 max_chars 的长句按子句标点再断，标点保留。"""
    policy = OutputPolicy(split=True, max_segments=4, max_chars=12)
    segments = split_reply("那个后藤想了想，其实也没有很难，试试看嘛", policy)
    # 子句贪心装箱：装得下就并在一条，装不下才另起
    assert [s for s, _ in segments] == ["那个后藤想了想，", "其实也没有很难，试试看嘛"]
    assert all(len(s) <= 12 for s, _ in segments)

    # 找不到可断的标点：宁可超长也不在词中间硬切
    long_run = "后" * 30
    assert [s for s, _ in split_reply(long_run, policy)] == [long_run]

    # max_chars=0 关闭长句拆分
    off = OutputPolicy(split=True, max_segments=4, max_chars=0)
    assert len(split_reply("那个后藤想了想，其实也没有很难", off)) == 1


def test_split_reply_respects_explicit_newlines_below_length_threshold():
    """模型主动换行表示说了两段，不应被短回复阈值合回一条消息。"""
    policy = OutputPolicy(
        split=True,
        split_min_chars=40,
        max_segments=4,
        max_chars=30,
    )
    reply = (
        "呜...17小时硬座也太折磨了\n\n"
        "还是去问问乘务员吧，万一能补到卧铺呢"
    )

    assert len(reply) < policy.split_min_chars
    assert [s for s, _ in split_reply(reply, policy)] == [
        "呜...17小时硬座也太折磨了",
        "还是去问问乘务员吧，万一能补到卧铺呢",
    ]


def test_is_skip_recognises_marker_variants():
    from botcchi.service import is_skip

    for text in ("[skip]", " [skip] ", "[SKIP]", "【skip】", "skip", "[skip]。"):
        assert is_skip(text), text
    # 正文里出现 skip 不算——只有整条就是标记时才跳过
    for text in ("[skip] 那个...后藤不想说", "后藤会skip这个话题的", ""):
        assert not is_skip(text), text


async def test_skip_marker_sends_nothing(mock_openai, tmp_path):
    """模型输出 [skip]：不发消息、不写历史。"""
    from botcchi.app import build_context

    mock_openai.enqueue(text_chunks("[skip]"))
    ctx = build_context(make_settings(tmp_path), client=mock_openai.client())
    channel = FakeChannel()
    incoming = IncomingMessage(
        session=SessionKey(channel="fake", chat_type="private", chat_id="10001"),
        user_id="10001", user_name="hhk", text="你这个笨蛋机器人",
    )

    await ctx.service.handle(incoming, channel)
    assert channel.sent == []
    assert await ctx.service.store.get(incoming.session) == []
    await ctx.shutdown()


def test_onebot_parse_group_at_message():
    adapter = OneBotAdapter(OneBotSettings(), on_message=None)  # type: ignore[arg-type]
    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 123456,
        "user_id": 654321,
        "message_id": 42,
        "sender": {"nickname": "hhk", "card": "群名片"},
        "message": [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": " 你好呀"}},
            {"type": "image", "data": {"url": "http://img.example/1.jpg"}},
        ],
    }
    incoming = adapter._parse_message_event(event, self_id="999")
    assert incoming is not None
    assert incoming.session.chat_type == "group"
    assert incoming.session.chat_id == "123456"
    assert incoming.at_me is True
    assert incoming.text == "你好呀"
    assert incoming.images == ["http://img.example/1.jpg"]
    assert incoming.user_name == "群名片"


async def test_direct_message_provider_failure_sends_notice(mock_openai, tmp_path):
    """@/私聊必回场景下 provider 挂掉不应静默，要给出失败提示。"""
    mock_openai.enqueue_status(500)
    service = build_context(make_settings(tmp_path), client=mock_openai.client()).service
    channel = FakeChannel()

    session = SessionKey(channel="fake", chat_type="private", chat_id="10001")
    await service.handle(
        IncomingMessage(
            session=session, user_id="10001", user_name="hhk", text="在吗", at_me=True
        ),
        channel,
    )
    assert len(channel.sent) == 1
    assert "连不上" in channel.sent[0][1]
    # 失败的轮次不应写入历史
    assert await service.store.get(session) == []
