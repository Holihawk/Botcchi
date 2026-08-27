"""复读机：状态机语义 + 消息链路集成（文本与图片）。"""

from test_service import FakeChannel, make_settings

from botcchi.app import build_context
from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.config import RepeatSettings
from botcchi.trigger.repeater import Repeater, content_key


def make_repeater(**overrides) -> Repeater:
    defaults: dict = {
        "enabled": True,
        "trigger_count": 3,
        "probability": 1.0,
        "break_probability": 0.0,
    }
    defaults.update(overrides)
    return Repeater(RepeatSettings(**defaults))


def feed_text(r: Repeater, group: str, text: str):
    return r.feed(group, content_key(text, []))


def test_repeats_after_threshold_once():
    r = make_repeater()
    assert feed_text(r, "g", "哈哈") is None
    assert feed_text(r, "g", "哈哈") is None
    assert feed_text(r, "g", "哈哈") == "repeat"   # 第 3 条触发
    assert feed_text(r, "g", "哈哈") is None       # 每轮只复读一次
    assert feed_text(r, "g", "哈哈") is None


def test_streak_resets_on_different_text():
    r = make_repeater()
    feed_text(r, "g", "哈哈")
    feed_text(r, "g", "哈哈")
    assert feed_text(r, "g", "呵呵") is None       # 断掉重新计数
    assert feed_text(r, "g", "呵呵") is None
    assert feed_text(r, "g", "呵呵") == "repeat"


def test_image_key_participates():
    r = make_repeater()
    key = content_key("", ["ABCDEF.jpg"])
    assert r.feed("g", key) is None
    assert r.feed("g", key) is None
    assert r.feed("g", key) == "repeat"
    # 同文本不同图不算复读
    r2 = make_repeater()
    r2.feed("g", content_key("看", ["a.jpg"]))
    r2.feed("g", content_key("看", ["b.jpg"]))
    assert r2.feed("g", content_key("看", ["c.jpg"])) is None


def test_empty_content_ignored():
    r = make_repeater()
    for _ in range(5):
        assert r.feed("g", content_key("", [])) is None


def test_groups_are_isolated():
    r = make_repeater()
    feed_text(r, "g1", "哈哈")
    feed_text(r, "g1", "哈哈")
    assert feed_text(r, "g2", "哈哈") is None      # g2 才第 1 条
    assert feed_text(r, "g1", "哈哈") == "repeat"


def test_break_action():
    r = make_repeater(break_probability=1.0, break_texts=["打断施法！"])
    feed_text(r, "g", "哈哈")
    feed_text(r, "g", "哈哈")
    assert feed_text(r, "g", "哈哈") == "break"
    assert r.break_text() == "打断施法！"
    # 打断后重新计数
    assert feed_text(r, "g", "哈哈") is None
    assert feed_text(r, "g", "哈哈") is None
    assert feed_text(r, "g", "哈哈") == "break"


def test_probability_zero_never_repeats():
    r = make_repeater(probability=0.0)
    for _ in range(5):
        assert feed_text(r, "g", "哈哈") is None


def test_disabled():
    r = make_repeater(enabled=False)
    for _ in range(5):
        assert feed_text(r, "g", "哈哈") is None


class ImageFakeChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.images: list[tuple[SessionKey, str]] = []

    async def send_image(self, session, file):
        self.images.append((session, file))


def repeat_settings() -> RepeatSettings:
    return RepeatSettings(
        enabled=True, trigger_count=3, probability=1.0, break_probability=0.0
    )


async def test_service_text_repeat(mock_openai, tmp_path):
    """群里三条相同消息 → 复读一条，不经 LLM。"""
    settings = make_settings(tmp_path)
    settings.trigger.repeat = repeat_settings()
    service = build_context(settings, client=mock_openai.client()).service
    channel = ImageFakeChannel()
    session = SessionKey(channel="fake", chat_type="group", chat_id="777")

    def msg(uid: str, text: str) -> IncomingMessage:
        return IncomingMessage(
            session=session, user_id=uid, user_name=f"u{uid}", text=text
        )

    await service.handle(msg("1", "太真实了"), channel)
    await service.handle(msg("2", "太真实了"), channel)
    assert channel.sent == []
    await service.handle(msg("3", "太真实了"), channel)
    assert [text for _, text, _ in channel.sent] == ["太真实了"]
    assert mock_openai.requests == []  # 未触发 LLM

    await service.handle(msg("4", "太真实了"), channel)
    assert len(channel.sent) == 1  # 不再重复


async def test_service_image_repeat(mock_openai, tmp_path):
    """三条相同图片（同 file 指纹、URL 不同）→ 复读原图。"""
    settings = make_settings(tmp_path)
    settings.trigger.repeat = repeat_settings()
    service = build_context(settings, client=mock_openai.client()).service
    channel = ImageFakeChannel()
    session = SessionKey(channel="fake", chat_type="group", chat_id="777")

    def img_msg(uid: str, url: str) -> IncomingMessage:
        return IncomingMessage(
            session=session,
            user_id=uid,
            user_name=f"u{uid}",
            text="",
            images=[url],
            image_keys=["MD5HASH.jpg"],
        )

    await service.handle(img_msg("1", "http://img/a1"), channel)
    await service.handle(img_msg("2", "http://img/a2"), channel)
    assert channel.images == []
    await service.handle(img_msg("3", "http://img/a3"), channel)
    assert channel.images == [(session, "http://img/a3")]  # 转发当前这张
    assert channel.sent == []  # 纯图片复读不发文本
    assert mock_openai.requests == []
