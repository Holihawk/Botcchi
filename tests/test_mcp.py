"""MCP 客户端：stdio 服务器连接、工具注册与 agent loop 调用。"""

import sys
from pathlib import Path

from conftest import text_chunks, tool_call_chunks

from botcchi.agent.assistant import Assistant
from botcchi.agent.events import ToolResultEvent, TurnComplete
from botcchi.agent.loop import AgentLoop
from botcchi.agent.messages import system, user
from botcchi.config import MCPServerSettings, ProviderSettings
from botcchi.providers.registry import ProviderRegistry
from botcchi.tools.mcp import MCPManager
from botcchi.tools.registry import ToolRegistry

SERVER = str(Path(__file__).parent / "mcp_test_server.py")


def stdio_config(name: str = "testsrv") -> MCPServerSettings:
    return MCPServerSettings(
        name=name, transport="stdio", command=sys.executable, args=[SERVER]
    )


async def test_stdio_server_tools_registered_and_callable():
    registry = ToolRegistry()
    manager = MCPManager([stdio_config()])
    try:
        await manager.start(registry)

        state = manager.states["testsrv"]
        assert state.connected is True
        assert set(state.tools) == {"testsrv_echo", "testsrv_add"}

        # 定义带远端 JSON Schema
        defs = {d["function"]["name"]: d for d in registry.definitions()}
        echo_params = defs["testsrv_echo"]["function"]["parameters"]
        assert "text" in echo_params["properties"]

        # 直接执行
        from botcchi.agent.messages import ToolCall

        result = await registry.execute(
            ToolCall(id="1", name="testsrv_echo", arguments='{"text": "你好"}')
        )
        assert result == "echo: 你好"
        result = await registry.execute(
            ToolCall(id="2", name="testsrv_add", arguments='{"a": 2, "b": 3}')
        )
        assert result == "5"
    finally:
        await manager.aclose()


async def test_agent_loop_calls_mcp_tool(mock_openai):
    registry = ToolRegistry()
    manager = MCPManager([stdio_config()])
    try:
        await manager.start(registry)
        providers = ProviderRegistry.from_settings(
            [ProviderSettings(name="mock", api_base="http://mock/v1", api_keys=["k"])],
            client=mock_openai.client(),
        )
        loop = AgentLoop(providers, registry)
        assistant = Assistant(
            name="t", prompt="p", model="mock/m", tools=["testsrv_add"]
        )

        mock_openai.enqueue(tool_call_chunks("testsrv_add", '{"a": 40, "b": 2}'))
        mock_openai.enqueue(text_chunks("答案是42"))

        events = [e async for e in loop.run(assistant, [system("p"), user("算一下")])]

        tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
        assert tool_results[0].result == "42"
        assert events[-1].message.text() == "答案是42"
        assert isinstance(events[-1], TurnComplete)
        # 发给模型的工具定义来自 MCP 的 inputSchema
        sent_tools = mock_openai.requests[0]["tools"]
        assert sent_tools[0]["function"]["name"] == "testsrv_add"
    finally:
        await manager.aclose()


async def test_failed_server_does_not_break_startup():
    registry = ToolRegistry()
    manager = MCPManager(
        [
            MCPServerSettings(
                name="broken",
                transport="stdio",
                command="/nonexistent-command-xyz",
            ),
            stdio_config("good"),
        ]
    )
    try:
        await manager.start(registry)
        assert manager.states["broken"].connected is False
        assert manager.states["broken"].error
        assert manager.states["good"].connected is True
    finally:
        await manager.aclose()
