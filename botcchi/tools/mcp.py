"""MCP 客户端：把外部 MCP 服务器的工具挂进 ToolRegistry。

支持 stdio（子进程）与 streamable HTTP 两种传输。工具以
"<服务器名>_<工具名>" 注册，人格勾选后即可被 agent 调用——对
Agent Loop 而言与内置工具完全无差别。
"""

import asyncio
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from ..config import MCPServerSettings
from .registry import Tool, ToolRegistry

CONNECT_TIMEOUT = 20.0
CALL_TIMEOUT = 60.0

_NAME_SANITIZE = re.compile(r"[^a-zA-Z0-9_-]")


def _tool_name(server: str, tool: str) -> str:
    return _NAME_SANITIZE.sub("_", f"{server}_{tool}")[:64]


@dataclass
class MCPServerState:
    name: str
    connected: bool = False
    tools: list[str] = field(default_factory=list)
    error: str | None = None


class MCPManager:
    def __init__(self, servers: list[MCPServerSettings]):
        self.configs = servers
        self.states: dict[str, MCPServerState] = {}
        self._stack = AsyncExitStack()

    async def start(self, registry: ToolRegistry) -> None:
        """连接所有启用的服务器并注册其工具。单个失败不影响其余与启动"""
        for cfg in self.configs:
            state = MCPServerState(name=cfg.name)
            self.states[cfg.name] = state
            if not cfg.enabled:
                continue
            try:
                session = await asyncio.wait_for(
                    self._connect(cfg), timeout=CONNECT_TIMEOUT
                )
                listed = await asyncio.wait_for(
                    session.list_tools(), timeout=CONNECT_TIMEOUT
                )
                for t in listed.tools:
                    tool = self._make_tool(cfg.name, session, t)
                    if registry.get(tool.name) is not None:
                        logger.warning(f"MCP 工具重名，覆盖注册: {tool.name}")
                    registry.register(tool)
                    state.tools.append(tool.name)
                state.connected = True
                logger.info(
                    f"MCP [{cfg.name}] 已连接，注册 {len(state.tools)} 个工具: "
                    f"{state.tools}"
                )
            except Exception as e:
                state.error = str(e)
                logger.warning(f"MCP [{cfg.name}] 连接失败（跳过）: {e}")

    async def _connect(self, cfg: MCPServerSettings) -> ClientSession:
        if cfg.transport == "stdio":
            if not cfg.command:
                raise ValueError("stdio 服务器缺少 command")
            params = StdioServerParameters(
                command=cfg.command,
                args=cfg.args,
                env={**get_default_environment(), **cfg.env},
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
        else:
            if not cfg.url:
                raise ValueError("http 服务器缺少 url")
            read, write, _ = await self._stack.enter_async_context(
                streamablehttp_client(cfg.url, headers=cfg.headers or None)
            )
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    def _make_tool(self, server: str, session: ClientSession, t: Any) -> Tool:
        remote_name = t.name

        async def call(**kwargs: Any) -> str:
            result = await asyncio.wait_for(
                session.call_tool(remote_name, kwargs), timeout=CALL_TIMEOUT
            )
            parts: list[str] = []
            for content in result.content:
                if getattr(content, "type", "") == "text":
                    parts.append(content.text)
                elif getattr(content, "type", "") == "image":
                    parts.append("[工具返回了图片，暂不支持展示]")
            text = "\n".join(parts).strip() or "（无输出）"
            if getattr(result, "isError", False):
                return f"工具执行出错: {text}"
            return text

        return Tool(
            func=call,
            name=_tool_name(server, remote_name),
            description=f"[{server}] {t.description or remote_name}",
            params_model=None,
            parameters=t.inputSchema
            or {"type": "object", "properties": {}},
        )

    async def aclose(self) -> None:
        try:
            await self._stack.aclose()
        except Exception as e:
            logger.warning(f"关闭 MCP 连接时出错: {e}")
