"""Provider contribution pack."""

from __future__ import annotations

from ..contributions import FileContribution
from ..ir import CompilerIR
from .common import file, py


class ProviderPack:
    capability_id = "providers"

    def contribute(self, ir: CompilerIR) -> tuple[FileContribution, ...]:
        files = [
            file(
                f"src/{ir.package}/providers.py",
                _provider_module(ir),
                f"provider.{ir.provider.kind}",
            )
        ]
        if ir.provider.kind == "custom":
            files.append(
                file(
                    f"src/{ir.package}/integrations/custom_provider.py",
                    _custom_provider_module(),
                    "provider.custom.skeleton",
                )
            )
        return tuple(files)


def _provider_module(ir: CompilerIR) -> str:
    provider = ir.provider
    common = '''\
"""Provider construction from environment-variable references."""

from __future__ import annotations
'''
    if provider.kind == "custom":
        return (
            common
            + f"""\

from {ir.package}.integrations.custom_provider import CustomProvider


def build_provider() -> CustomProvider:
    return CustomProvider()
"""
        )

    class_map = {
        "openai_responses": ("OpenAIResponsesProvider", "OpenAIResponsesProviderOptions"),
        "openai_chat": (
            "OpenAIChatCompletionsProvider",
            "OpenAIChatProviderOptions",
        ),
        "anthropic": ("AnthropicProvider", "AnthropicProviderOptions"),
        "gemini": ("GeminiProvider", "GeminiProviderOptions"),
        "llama_cpp": ("LlamaCppProvider", "LlamaCppProviderOptions"),
        "vllm": ("VLLMProvider", "VLLMProviderOptions"),
        "sglang": ("SGLangProvider", "SGLangProviderOptions"),
    }
    provider_class, options_class = class_map[provider.kind]
    args: list[str] = []
    if provider.api_key_env:
        args.append(f"api_key=os.environ.get({py(provider.api_key_env)})")
    if provider.base_url_env:
        args.append(f"base_url=os.environ.get({py(provider.base_url_env)})")
    if provider.kind == "gemini":
        if provider.project_env:
            args.append(f"project=os.environ.get({py(provider.project_env)})")
        if provider.location:
            args.append(f"location={py(provider.location)}")
    if provider.kind in {"llama_cpp", "vllm", "sglang"} and provider.context_window:
        args.append(f"context_window={provider.context_window}")
    compact_options = f"{options_class}({', '.join(args)})"
    compact_call = f"{provider_class}({compact_options})"
    os_import = (
        "\nimport os\n"
        if provider.api_key_env
        or provider.base_url_env
        or (provider.kind == "gemini" and provider.project_env)
        else ""
    )
    if len(f"    return {compact_call}") <= 100:
        provider_call = compact_call
    else:
        rendered_args = "\n".join(f"            {item}," for item in args)
        options = f"{options_class}(\n{rendered_args}\n        )"
        provider_call = f"{provider_class}(\n        {options},\n    )"
    return (
        common
        + os_import
        + f'''\

from linch import {provider_class}, {options_class}


def build_provider() -> {provider_class}:
    """Create the project's single configured provider."""
    return {provider_call}
'''
    )


def _custom_provider_module() -> str:
    return '''\
"""Custom provider adapter skeleton.

Implement the public ``BaseProvider`` duck-typed contract. Request/event values
are intentionally typed as ``Any`` because generated code imports only top-level
Linch symbols and does not depend on private DTO module paths.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from linch import BaseProvider, ProviderCapabilities


class CustomProvider(BaseProvider):
    id = "custom"

    def context_window(self, model: str) -> int:
        return 128_000

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(context_window=self.context_window(model))

    async def stream(self, request: Any) -> AsyncIterator[dict[str, object]]:
        raise NotImplementedError("TODO: implement CustomProvider.stream")
        if False:  # pragma: no cover - keeps this an async generator skeleton
            yield {}
'''


__all__ = ["ProviderPack"]
