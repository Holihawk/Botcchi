"""
Agent Loop：多轮工具调用的流式对话循环，channel 无关
支持模型降级链：失败后，按 fallback_models顺序尝试下一个模型
跨 provider 的顺序即优先级
"""

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx
from loguru import logger

from ..providers.base import ProviderError
from ..providers.registry import ProviderRegistry
from .assistant import Assistant
from .events import AgentEvent, ToolResultEvent, TurnComplete
from .messages import ChatMessage, tool_result

if TYPE_CHECKING:
    # 仅类型标注：运行时导入会构成 tools.registry ↔ agent 循环
    from ..tools.registry import ToolContext, ToolRegistry


class AgentLoop:
    def __init__(self, providers: ProviderRegistry, tools: "ToolRegistry"):
        self.providers = providers
        self.tools = tools

    async def run(
        self,
        assistant: Assistant,
        messages: list[ChatMessage],
        tool_ctx: "ToolContext | None" = None,
    ) -> AsyncIterator[AgentEvent]:
        """执行一次完整对话回合。

        messages: system + 短期历史 + 当前用户消息。
        tool_ctx: 会话上下文；缺省时上下文工具（签到等）对模型不可见。
        事件流以最后一轮的 TurnComplete 结束（其 message 即最终回复）。
        """
        candidates = [assistant.model, *assistant.fallback_models]
        last_error: Exception | None = None
        for index, model_ref in enumerate(candidates):
            try:
                produced = False
                async for event in self._run_with_model(
                    assistant, messages, model_ref, tool_ctx
                ):
                    produced = produced or isinstance(event, TurnComplete)
                    yield event
                return
            except (ProviderError, httpx.HTTPError) as e:
                if produced:
                    # 已有轮次成功，中途失败不再换模型（上下文已被消费）
                    raise
                last_error = e
                if index < len(candidates) - 1:
                    logger.warning(
                        f"模型 {model_ref} 不可用（{e}），"
                        f"降级到 {candidates[index + 1]}"
                    )
        raise last_error or ProviderError("没有可用的模型")

    async def _run_with_model(
        self,
        assistant: Assistant,
        messages: list[ChatMessage],
        model_ref: str,
        tool_ctx: "ToolContext | None" = None,
    ) -> AsyncIterator[AgentEvent]:
        provider, model_name, detail = self.providers.resolve(model_ref)
        temperature = (
            assistant.temperature
            if assistant.temperature is not None
            else detail.temperature
        )
        max_tokens = (
            assistant.max_tokens
            if assistant.max_tokens is not None
            else detail.max_tokens
        )
        tool_names = assistant.tools
        if tool_names and tool_ctx is not None and tool_ctx.disabled_tools:
            # 权限体系禁用的工具不进 schema，模型不会看到也不会尝试
            tool_names = [n for n in tool_names if n not in tool_ctx.disabled_tools]
        tool_defs = (
            self.tools.definitions(
                tool_names, include_context_tools=tool_ctx is not None
            )
            if tool_names
            else None
        ) or None

        context = list(messages)
        for round_index in range(assistant.max_tool_rounds + 1):
            # 最后一轮不再提供工具，强制模型收敛为文本回复
            allow_tools = tool_defs if round_index < assistant.max_tool_rounds else None
            turn: TurnComplete | None = None
            async for event in provider.stream_chat(
                model=model_name,
                messages=context,
                tools=allow_tools,
                temperature=temperature,
                max_tokens=max_tokens,
            ):
                yield event
                if isinstance(event, TurnComplete):
                    turn = event
            if turn is None:
                raise RuntimeError("provider 未产生 TurnComplete 事件")

            context.append(turn.message)
            if not turn.message.tool_calls:
                return

            for call in turn.message.tool_calls:
                logger.info(f"工具调用: {call.name}({call.arguments})")
                result = await self.tools.execute(call, tool_ctx)
                yield ToolResultEvent(id=call.id, name=call.name, result=result)
                context.append(tool_result(call.id, result))
