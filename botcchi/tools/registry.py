"""工具注册表：Python 函数 → function calling 定义与执行。

工具函数把首参声明为 `ctx: ToolContext` 即成为「上下文工具」：执行时注入
当前会话上下文（发起用户、channel 句柄），可主动发图/发消息；ctx 不出现在
暴露给 LLM 的参数 schema 中，也无法被 LLM 伪造。无上下文的调用场景
（如伪人模式）会自动隐藏这类工具。
"""

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic import BaseModel, create_model

from ..agent.messages import ToolCall

if TYPE_CHECKING:
    from ..channels.base import Channel, IncomingMessage
    from ..social.shop import ToolCharger


@dataclass(slots=True)
class ToolContext:
    """一次工具调用所处的会话上下文（由 ChatService 每次请求构造）"""

    incoming: "IncomingMessage"
    channel: "Channel"
    bot_name: str = ""
    disabled_tools: frozenset[str] = frozenset()
    """本会话被权限体系禁用的工具：从 schema 隐藏，执行时也硬性拒绝"""
    charger: "ToolCharger | None" = None
    """金币工具调用费：配置了费用的工具执行前扣费（超管免费）"""
    stats: Any = None
    """调用统计（StatsService），配置了数据库时注入"""


class Tool:
    def __init__(
        self,
        func: Callable[..., Any],
        name: str,
        description: str,
        params_model: type[BaseModel] | None,
        parameters: dict[str, Any] | None = None,
        needs_context: bool = False,
    ):
        """parameters: 直接给定的 JSON Schema（如 MCP 工具），优先于 params_model；
        此时参数不做本地校验，原样透传给 func(**kwargs)"""
        self.func = func
        self.name = name
        self.description = description
        self.params_model = params_model
        self.parameters = parameters
        self.needs_context = needs_context

    def definition(self) -> dict[str, Any]:
        """OpenAI function calling 格式的工具定义。"""
        if self.parameters is not None:
            parameters = self.parameters
        elif self.params_model is None:
            parameters = {"type": "object", "properties": {}}
        else:
            schema = self.params_model.model_json_schema()
            parameters = {
                "type": "object",
                "properties": schema.get("properties", {}),
                "required": schema.get("required", []),
            }
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }

    async def execute(self, arguments: str, ctx: ToolContext | None = None) -> str:
        kwargs: dict[str, Any] = {}
        if self.params_model is not None:
            raw = json.loads(arguments) if arguments.strip() else {}
            kwargs = self.params_model.model_validate(raw).model_dump()
        elif self.parameters is not None:
            kwargs = json.loads(arguments) if arguments.strip() else {}
        if self.needs_context:
            if ctx is None:
                return f"错误：工具 {self.name} 需要会话上下文，当前场景不可用"
            kwargs["ctx"] = ctx
        result = self.func(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)


def _build_params_model(
    func: Callable[..., Any], name: str
) -> tuple[type[BaseModel] | None, bool]:
    """构建参数模型；首参为 ctx 时标记为上下文工具并从 schema 中剔除"""
    fields: dict[str, Any] = {}
    needs_context = False
    for i, (param_name, param) in enumerate(
        inspect.signature(func).parameters.items()
    ):
        if i == 0 and param_name == "ctx":
            needs_context = True
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        annotation = (
            param.annotation if param.annotation is not inspect.Parameter.empty else str
        )
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[param_name] = (annotation, default)
    model = create_model(f"{name}_params", **fields) if fields else None
    return model, needs_context


def tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Any:
    """把函数标记为 agent 工具。描述取自参数或 docstring。"""

    def decorator(f: Callable[..., Any]) -> Tool:
        tool_name = name or f.__name__
        params_model, needs_context = _build_params_model(f, tool_name)
        return Tool(
            func=f,
            name=tool_name,
            description=description or inspect.getdoc(f) or tool_name,
            params_model=params_model,
            needs_context=needs_context,
        )

    return decorator(func) if func is not None else decorator


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, t: Tool) -> None:
        self._tools[t.name] = t

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(
        self,
        names: list[str] | None = None,
        include_context_tools: bool = True,
    ) -> list[dict[str, Any]]:
        """取工具定义；names 为 None 时返回全部。

        include_context_tools=False 时隐藏上下文工具（无 ToolContext 的场景）。
        """
        tools = (
            self._tools.values()
            if names is None
            else [self._tools[n] for n in names if n in self._tools]
        )
        return [
            t.definition()
            for t in tools
            if include_context_tools or not t.needs_context
        ]

    async def execute(self, call: ToolCall, ctx: ToolContext | None = None) -> str:
        t = self._tools.get(call.name)
        if t is None:
            return f"错误：不存在名为 {call.name} 的工具"
        if ctx is not None and call.name in ctx.disabled_tools:
            # schema 过滤的兜底：即使模型仍尝试调用也硬性拒绝
            return f"错误：{call.name} 对应的功能在当前会话已被关闭"
        if ctx is not None and ctx.stats is not None:
            await ctx.stats.record("tool", call.name, ctx.incoming)
        charged = 0
        if ctx is not None and ctx.charger is not None:
            charged, refusal = await ctx.charger.take(
                call.name, ctx.incoming.user_id
            )
            if refusal is not None:
                return refusal
        try:
            return await t.execute(call.arguments, ctx)
        except Exception as e:
            logger.exception(f"工具 {call.name} 执行失败")
            if ctx is not None and ctx.charger is not None and charged:
                await ctx.charger.refund(call.name, ctx.incoming.user_id, charged)
            return f"工具执行失败: {e}"
