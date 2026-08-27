"""内置工具"""

from datetime import datetime

from ..registry import Tool, ToolRegistry, tool


@tool(description="获取当前的日期、时间和星期")
def current_time() -> str:
    now = datetime.now()
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    return f"{now:%Y-%m-%d %H:%M:%S} 星期{weekdays[now.weekday()]}"


BUILTIN_TOOLS: list[Tool] = [current_time]


def register_builtin(registry: ToolRegistry) -> None:
    for t in BUILTIN_TOOLS:
        registry.register(t)
