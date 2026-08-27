"""命令行入口：serve 启动服务；chat 在终端直接对话（调试人格/provider 用）"""

import argparse
import asyncio
import sys
from pathlib import Path

from loguru import logger


def _setup_logging(log_dir: str | None, level: str, retention_days: int) -> None:
    """按 level 重建控制台 sink，并追加两份按天轮转的文件 sink：
    全量日志（按 level）与错误日志（仅 ERROR 及以上），沿用旧版分级方案。

    loguru 自带的默认 sink 是写死 DEBUG 的，不换掉的话 log.level 只管得到
    文件、管不到控制台——终端会一直刷 DEBUG 级的协议事件。
    """
    logger.remove()
    logger.add(sys.stderr, level=level)
    if not log_dir:
        return
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    common = {
        "rotation": "00:00",  # 每天零点切一个新文件
        "retention": f"{retention_days} days",
        "encoding": "utf-8",
        "enqueue": True,  # 异步入队，多协程写日志安全
    }
    logger.add(
        str(Path(log_dir) / "{time:YYYY-MM-DD}.log"), level=level, **common
    )
    logger.add(
        str(Path(log_dir) / "error_{time:YYYY-MM-DD}.log"),
        level="ERROR",
        **common,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="botcchi")
    parser.add_argument("-c", "--config", default=None, help="配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="启动服务（OneBot 反向 WS + API）")
    chat_parser = sub.add_parser("chat", help="终端交互式对话（调试用）")
    chat_parser.add_argument("-a", "--assistant", default=None, help="指定 Assistant")
    args = parser.parse_args()

    from .config import load_settings

    settings = load_settings(args.config)
    _setup_logging(
        settings.log.dir, settings.log.level, settings.log.retention_days
    )

    if args.command == "serve":
        import uvicorn

        from .app import create_app

        uvicorn.run(
            create_app(settings),
            host=settings.server.host,
            port=settings.server.port,
        )
    elif args.command == "chat":
        asyncio.run(_chat(settings, args.assistant))


async def _chat(settings, assistant_name: str | None) -> None:
    from .agent.events import ReasoningDelta, TextDelta, ToolCallEvent, ToolResultEvent
    from .agent.messages import ChatMessage
    from .app import build_context
    from .channels.base import IncomingMessage, SessionKey

    ctx = build_context(settings)
    await ctx.startup()
    service = ctx.service
    name = assistant_name or settings.default_assistant
    assistant = service.assistants.get(name)
    if assistant is None:
        logger.error(f"Assistant 不存在: {name}")
        sys.exit(1)

    session = SessionKey(channel="cli", chat_type="web", chat_id="cli")
    print(f"与 {assistant.nickname} 对话中（exit 退出，reset 重置）\n")
    try:
        while True:
            try:
                text = (await asyncio.to_thread(input, "你> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text == "exit":
                break
            if text == "reset":
                await service.store.reset(session)
                print("（会话已重置）")
                continue

            incoming = IncomingMessage(
                session=session, user_id="cli", user_name="user", text=text
            )
            from .agent.context import build_system_message, build_user_message

            history = await service.store.get(session)
            messages = [
                build_system_message(assistant, incoming),
                *history,
                build_user_message(incoming),
            ]
            print(f"{assistant.nickname}> ", end="", flush=True)
            reasoning_open = False
            final_text = ""
            async for event in service.loop.run(assistant, messages):
                if isinstance(event, ReasoningDelta):
                    if not reasoning_open:
                        print("\x1b[2m", end="")
                        reasoning_open = True
                    print(event.text, end="", flush=True)
                elif isinstance(event, TextDelta):
                    if reasoning_open:
                        print("\x1b[0m\n", end="")
                        reasoning_open = False
                    print(event.text, end="", flush=True)
                    final_text += event.text
                elif isinstance(event, ToolCallEvent):
                    if reasoning_open:
                        print("\x1b[0m\n", end="")
                        reasoning_open = False
                    print(f"\x1b[33m[调用工具 {event.name}({event.arguments})]\x1b[0m")
                elif isinstance(event, ToolResultEvent):
                    print(f"\x1b[33m[工具结果: {event.result[:200]}]\x1b[0m")
                    print(f"{assistant.nickname}> ", end="", flush=True)
            print()
            if final_text.strip():
                await service.store.append(
                    session,
                    ChatMessage(role="user", content=text),
                    ChatMessage(role="assistant", content=final_text.strip()),
                )
    finally:
        await ctx.shutdown()


if __name__ == "__main__":
    main()
