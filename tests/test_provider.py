"""Provider 层：流式解析、reasoning 分离、429 换 key。"""

import pytest
from conftest import text_chunks

from botcchi.agent.events import ReasoningDelta, TextDelta, TurnComplete
from botcchi.agent.messages import user
from botcchi.config import ProviderSettings
from botcchi.providers.base import ProviderError
from botcchi.providers.openai_compat import OpenAICompatProvider


def make_provider(mock, keys=None) -> OpenAICompatProvider:
    settings = ProviderSettings(
        name="mock",
        api_base="http://mock/v1",
        api_keys=keys or ["key-a"],
    )
    return OpenAICompatProvider(settings, client=mock.client())


async def collect(provider, **kwargs):
    events = []
    async for event in provider.stream_chat(**kwargs):
        events.append(event)
    return events


async def test_stream_text_and_reasoning(mock_openai):
    mock_openai.enqueue(text_chunks("你好呀！今天心情不错呢", reasoning="用户在打招呼"))
    provider = make_provider(mock_openai)

    events = await collect(
        provider, model="m", messages=[user("你好")]
    )

    reasoning = "".join(e.text for e in events if isinstance(e, ReasoningDelta))
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert reasoning == "用户在打招呼"
    assert text == "你好呀！今天心情不错呢"

    final = events[-1]
    assert isinstance(final, TurnComplete)
    assert final.message.text() == "你好呀！今天心情不错呢"
    assert final.message.reasoning == "用户在打招呼"
    assert final.message.tool_calls is None


async def test_429_rotates_key(mock_openai):
    mock_openai.enqueue_status(429)
    mock_openai.enqueue(text_chunks("ok"))
    provider = make_provider(mock_openai, keys=["key-a", "key-b"])

    events = await collect(provider, model="m", messages=[user("hi")])

    assert isinstance(events[-1], TurnComplete)
    assert len(mock_openai.auth_headers) == 2
    assert mock_openai.auth_headers[0] != mock_openai.auth_headers[1]


async def test_non_retryable_error_raises(mock_openai):
    mock_openai.enqueue_status(400)
    provider = make_provider(mock_openai)

    with pytest.raises(ProviderError) as exc_info:
        await collect(provider, model="m", messages=[user("hi")])
    assert exc_info.value.status_code == 400


async def test_proxy_policy_builds_client():
    from botcchi.providers.openai_compat import build_http_client

    base = {"name": "p", "api_base": "http://x/v1", "api_keys": ["k"]}
    follow_env = build_http_client(ProviderSettings(**base))
    direct = build_http_client(ProviderSettings(**base, proxy=False))
    try:
        assert follow_env.trust_env is True
        assert direct.trust_env is False
    finally:
        await follow_env.aclose()
        await direct.aclose()
