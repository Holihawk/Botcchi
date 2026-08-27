"""Agent Loop：多轮工具调用闭环。"""

from conftest import text_chunks, tool_call_chunks

from botcchi.agent.assistant import Assistant
from botcchi.agent.events import ToolCallEvent, ToolResultEvent, TurnComplete
from botcchi.agent.loop import AgentLoop
from botcchi.agent.messages import system, user
from botcchi.config import ProviderSettings
from botcchi.providers.registry import ProviderRegistry
from botcchi.tools.registry import ToolRegistry, tool


@tool(description="查询指定城市天气")
def get_weather(city: str) -> str:
    return f"{city}：晴，26 度"


def make_loop(mock) -> AgentLoop:
    registry = ProviderRegistry.from_settings(
        [ProviderSettings(name="mock", api_base="http://mock/v1", api_keys=["k"])],
        client=mock.client(),
    )
    tools = ToolRegistry()
    tools.register(get_weather)
    return AgentLoop(registry, tools)


async def test_tool_call_round_trip(mock_openai):
    # 第一轮：模型请求调用工具；第二轮：基于工具结果给出最终回复
    mock_openai.enqueue(tool_call_chunks("get_weather", '{"city": "南京"}'))
    mock_openai.enqueue(text_chunks("南京今天晴，26度哦"))

    loop = make_loop(mock_openai)
    assistant = Assistant(
        name="t", prompt="p", model="mock/m", tools=["get_weather"]
    )

    events = []
    async for event in loop.run(assistant, [system("p"), user("南京天气怎么样")]):
        events.append(event)

    tool_calls = [e for e in events if isinstance(e, ToolCallEvent)]
    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    turns = [e for e in events if isinstance(e, TurnComplete)]

    assert len(tool_calls) == 1
    assert tool_calls[0].name == "get_weather"
    assert tool_results[0].result == "南京：晴，26 度"
    assert len(turns) == 2
    assert turns[-1].message.text() == "南京今天晴，26度哦"

    # 第二次请求应携带 assistant tool_calls 消息与 tool 结果
    second_request = mock_openai.requests[1]
    roles = [m["role"] for m in second_request["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert second_request["messages"][3]["content"] == "南京：晴，26 度"
    # 第一次请求应带工具定义
    assert mock_openai.requests[0]["tools"][0]["function"]["name"] == "get_weather"


async def test_plain_reply_without_tools(mock_openai):
    mock_openai.enqueue(text_chunks("只是聊聊天啦"))
    loop = make_loop(mock_openai)
    assistant = Assistant(name="t", prompt="p", model="mock/m")

    events = [e async for e in loop.run(assistant, [system("p"), user("你好")])]

    assert isinstance(events[-1], TurnComplete)
    assert events[-1].message.text() == "只是聊聊天啦"
    assert "tools" not in mock_openai.requests[0]
