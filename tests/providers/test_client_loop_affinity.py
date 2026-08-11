"""Cached provider clients must not outlive the event loop they were built on.

`AsyncOpenAI`/`AsyncAnthropic` bind their transport to the loop running at
construction. A provider reused from a second loop — the normal shape of a
Celery worker, which runs one loop per task — otherwise raises
``Event loop is closed``. Hosts had to keep their own loop-keyed provider cache
to work around this.

These tests build clients only; nothing here touches the network.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


def _providers() -> list[tuple[str, Any]]:
    from linch.openai_responses import OpenAIOptions, OpenAIResponsesClient
    from linch.providers.anthropic import AnthropicProvider, AnthropicProviderOptions
    from linch.providers.openai_chat import (
        OpenAIChatCompletionsProvider,
        OpenAIChatProviderOptions,
    )

    return [
        (
            "openai-chat",
            OpenAIChatCompletionsProvider(OpenAIChatProviderOptions(api_key="test-key")),
        ),
        ("anthropic", AnthropicProvider(AnthropicProviderOptions(api_key="test-key"))),
        ("openai-responses", OpenAIResponsesClient(OpenAIOptions(api_key="test-key"))),
    ]


@pytest.mark.parametrize("name", ["openai-chat", "anthropic", "openai-responses"])
def test_client_is_rebuilt_on_a_new_event_loop(name: str) -> None:
    provider = dict(_providers())[name]

    first = asyncio.run(provider._get_client())
    second = asyncio.run(provider._get_client())

    assert first is not second, "a client bound to a closed loop was handed out again"


@pytest.mark.parametrize("name", ["openai-chat", "anthropic", "openai-responses"])
def test_client_is_built_once_within_one_loop(name: str) -> None:
    """The common case must stay free: one loop, one client, no rebuild."""
    provider = dict(_providers())[name]

    async def twice() -> tuple[Any, Any]:
        return await provider._get_client(), await provider._get_client()

    first, second = asyncio.run(twice())
    assert first is second
