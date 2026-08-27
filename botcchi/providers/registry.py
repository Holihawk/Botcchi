"""Provider 注册表：按 "provider/model" 引用解析到具体适配器与模型参数"""

import httpx

from ..config import ModelDetail, ProviderSettings
from .base import ChatProvider, ProviderError
from .openai_compat import OpenAICompatProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ChatProvider] = {}
        self._settings: dict[str, ProviderSettings] = {}

    @classmethod
    def from_settings(
        cls,
        providers: list[ProviderSettings],
        client: httpx.AsyncClient | None = None,
    ) -> "ProviderRegistry":
        registry = cls()
        for settings in providers:
            registry._providers[settings.name] = OpenAICompatProvider(
                settings, client=client
            )
            registry._settings[settings.name] = settings
        return registry

    def resolve(self, model_ref: str) -> tuple[ChatProvider, str, ModelDetail]:
        """解析模型引用。

        标准格式 "provider/model"（model 部分允许再含 '/'，兼容 openrouter）。
        无前缀时自动匹配：优先找模型列表里声明了该名字的 provider；
        只配置了一个 provider 时直接归属它。
        """
        provider_name, _, model_name = model_ref.partition("/")
        if model_name and provider_name in self._providers:
            settings = self._settings[provider_name]
            detail = next(
                (m for m in settings.models if m.name == model_name),
                ModelDetail(name=model_name),
            )
            return self._providers[provider_name], model_name, detail

        # 无前缀（或前缀不是已配置的 provider）：按完整字符串匹配
        declared = [
            (name, m)
            for name, s in self._settings.items()
            for m in s.models
            if m.name == model_ref
        ]
        if len(declared) == 1:
            name, detail = declared[0]
            return self._providers[name], detail.name, detail
        if len(declared) > 1:
            owners = ", ".join(n for n, _ in declared)
            raise ProviderError(
                f"模型 {model_ref} 在多个 provider 中声明（{owners}），"
                f"请使用 provider/model 格式"
            )
        if len(self._providers) == 1 and "/" not in model_ref:
            name = next(iter(self._providers))
            return self._providers[name], model_ref, ModelDetail(name=model_ref)
        raise ProviderError(
            f"无法解析模型引用: {model_ref}（已配置 provider: "
            f"{list(self._providers) or '（无）'}，请使用 provider/model 格式）"
        )

    async def aclose(self) -> None:
        for provider in self._providers.values():
            await provider.aclose()
