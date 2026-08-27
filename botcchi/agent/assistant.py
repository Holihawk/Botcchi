"""Assistant：人格 + 模型参数 + 工具集 + 输出策略"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class OutputPolicy(BaseModel):
    """拟人化输出策略（QQ 端生效；网页端始终流式完整输出）"""

    split: bool = True
    """按句切分为多条消息发送"""
    split_min_chars: int = 0
    """回复短于此长度时不拆分（0 表示关闭阈值）"""
    max_segments: int = 4
    max_chars: int = 25
    """
    单条消息的字数上限：超过就按子句标点再断开（0 表示不限）
    下限交给提示词把控——硬把短句拼长只会让语气变味
    """
    delay_per_char: float = 0.2
    """每字符打字延迟（秒）"""
    max_delay: float = 3.0


class GreetingPolicy(BaseModel):
    """被单独召唤时的快速问候"""

    enabled: bool = True
    replies: list[str] = Field(default_factory=lambda: [
        "哦豁？！",
        "呜...后藤在呢",
        "欸？叫后藤做什么呀",
        "怎么啦？",
        "后藤有听到哦",
        "呀...是在叫后藤吗？",
        "后藤、后藤一直都在哦",
        "呜哇！突然被点名了...",
        "那、那个...有什么事吗？",
        "听、听见啦，不用喊第二遍...",
    ])
    cooldown_seconds: float = 10.0


class Assistant(BaseModel):
    name: str
    display_name: str = ""
    prompt: str
    model: str
    """格式: provider/model，如 deepseek/deepseek-chat"""
    fallback_models: list[str] = Field(default_factory=list)
    """主模型不可用时按顺序降级尝试的模型引用（可跨 provider，即优先级链）"""
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[str] = Field(default_factory=list)
    max_tool_rounds: int = 4
    policy: OutputPolicy = Field(default_factory=OutputPolicy)
    greeting: GreetingPolicy = Field(default_factory=GreetingPolicy)
    prompt_file: str | None = Field(default=None, exclude=True)
    """prompt 来源文件（相对 assistants 目录）；保存时回写到该文件"""

    @property
    def nickname(self) -> str:
        return self.display_name or self.name


def load_assistants(directory: Path) -> dict[str, Assistant]:
    """从目录加载全部 Assistant 定义（*.yaml，prompt 可内联或用 prompt_file 指向文件）"""
    assistants: dict[str, Assistant] = {}
    if not directory.is_dir():
        return assistants
    for path in sorted(directory.glob("*.yaml")):
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if prompt_file := data.get("prompt_file"):
            data["prompt"] = (directory / prompt_file).read_text(encoding="utf-8")
        elif "prompt" not in data:
            # 与 yaml 同名的 <name>.prompt.md 自动作为人格 prompt
            default_prompt = directory / f"{path.stem}.prompt.md"
            if default_prompt.exists():
                data["prompt"] = default_prompt.read_text(encoding="utf-8")
                data["prompt_file"] = default_prompt.name
        data.setdefault("name", path.stem)
        assistant = Assistant.model_validate(data)
        assistants[assistant.name] = assistant
    return assistants


def delete_assistant(directory: Path, assistant: Assistant) -> None:
    """删除 Assistant 的定义文件（yaml 与其 prompt 文件）"""
    (directory / f"{assistant.name}.yaml").unlink(missing_ok=True)
    if assistant.prompt_file:
        (directory / assistant.prompt_file).unlink(missing_ok=True)


def save_assistant(directory: Path, assistant: Assistant) -> None:
    """把 Assistant 定义写回磁盘（prompt 回写到其来源文件，其余进 yaml）"""
    data = assistant.model_dump(exclude_none=True)
    if assistant.prompt_file:
        (directory / assistant.prompt_file).write_text(
            assistant.prompt, encoding="utf-8"
        )
        data.pop("prompt", None)
        data["prompt_file"] = assistant.prompt_file
    with (directory / f"{assistant.name}.yaml").open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
