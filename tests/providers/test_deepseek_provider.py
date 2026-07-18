"""Request-shape tests for DeepSeek's native OpenAI-compatible provider."""

from __future__ import annotations

import pytest


def _request(**overrides):
    from linch.types import Message, ProviderRequest, TextBlock

    values = {
        "model": "deepseek-v4-flash",
        "system": [],
        "tools": [],
        "messages": [Message(role="user", content=[TextBlock(text="Return JSON.")])],
    }
    values.update(overrides)
    return ProviderRequest(**values)


def test_deepseek_payload_uses_native_thinking_and_json_object() -> None:
    from linch.providers.deepseek import DeepSeekProviderOptions, _build_deepseek_payload
    from linch.types import OutputSchema

    schema = OutputSchema(name="support_turn", schema={"type": "object", "properties": {}})
    payload = _build_deepseek_payload(
        _request(output_schema=schema),
        DeepSeekProviderOptions(effort="medium", extra_body={"trace": "keep"}),
    )

    assert payload["response_format"] == {"type": "json_object"}
    assert payload["reasoning_effort"] == "high"
    assert payload["extra_body"] == {
        "trace": "keep",
        "thinking": {"type": "enabled"},
    }


def test_deepseek_payload_turn_override_disables_thinking_and_effort() -> None:
    from linch.providers.deepseek import DeepSeekProviderOptions, _build_deepseek_payload

    payload = _build_deepseek_payload(
        _request(thinking={"type": "disabled"}),
        DeepSeekProviderOptions(thinking="enabled", effort="max"),
    )

    assert payload["extra_body"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload


def test_deepseek_payload_round_trips_reasoning_content_with_a_tool_call() -> None:
    from linch.providers.deepseek import _build_deepseek_payload
    from linch.types import Message, ThinkingBlock, ToolResultBlock, ToolUseBlock

    request = _request(
        messages=[
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(thinking="Need documentation before answering."),
                    ToolUseBlock(id="call_1", name="search_docs", input={"query": "workflow"}),
                ],
            ),
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id="call_1", content="relevant docs")],
            ),
        ]
    )

    payload = _build_deepseek_payload(request)

    assistant = payload["messages"][0]
    assert assistant["reasoning_content"] == "Need documentation before answering."
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert payload["messages"][1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "relevant docs",
    }


def test_deepseek_provider_has_no_terminal_schema_tool() -> None:
    from linch.providers import DeepSeekProvider

    caps = DeepSeekProvider().capabilities("deepseek-v4-flash")

    assert caps.structured_output is True
    assert caps.structured_output_terminal_tool is False


def test_deepseek_thinking_rejects_forced_tool_choice_but_allows_auto() -> None:
    from linch.errors import ProviderError
    from linch.providers.deepseek import _build_deepseek_payload

    with pytest.raises(ProviderError, match="does not support forced tool_choice"):
        _build_deepseek_payload(_request(tool_choice={"name": "search_docs"}))

    payload = _build_deepseek_payload(_request(tool_choice="auto"))
    assert payload["tool_choice"] == "auto"


def test_deepseek_disabled_thinking_allows_forced_tool_choice() -> None:
    from linch.providers.deepseek import _build_deepseek_payload

    payload = _build_deepseek_payload(
        _request(thinking={"type": "disabled"}, tool_choice={"name": "search_docs"})
    )

    assert payload["extra_body"]["thinking"] == {"type": "disabled"}
    assert payload["tool_choice"] == {
        "type": "function",
        "function": {"name": "search_docs"},
    }
