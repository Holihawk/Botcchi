from botcchi.agent.assistant import GreetingPolicy
from botcchi.channels.base import IncomingMessage, SessionKey
from botcchi.greeting import GreetingService


class FakeChannel:
    name = "fake"

    def __init__(self):
        self.sent = []

    async def send_text(self, session, text, **kwargs):
        self.sent.append(text)


class FakeStickers:
    def __init__(self):
        self.calls = []

    async def send_random(self, channel, session, emotions):
        self.calls.append(tuple(emotions))
        return True


async def test_greeting_always_requests_one_of_three_emotions():
    service = GreetingService()
    channel = FakeChannel()
    stickers = FakeStickers()
    incoming = IncomingMessage(
        session=SessionKey("fake", "group", "777"),
        user_id="1",
        user_name="u",
        text="波奇",
    )

    assert await service.greet(
        incoming,
        channel,  # type: ignore[arg-type]
        GreetingPolicy(cooldown_seconds=0),
        stickers,  # type: ignore[arg-type]
    )
    assert len(channel.sent) == 1
    assert stickers.calls == [("打招呼", "吃喝", "惊讶")]


def test_greeting_does_not_repeat_text_immediately():
    service = GreetingService()
    policy = GreetingPolicy(replies=["一", "二"], cooldown_seconds=0)
    key = "fake:group:777"

    first = service.choose(key, policy)
    second = service.choose(key, policy)

    assert first != second
