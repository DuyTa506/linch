"""Wire contracts for opt-in positional catalog changes, using offline SDK transports."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from linch.types import (
    CatalogChangeBlock,
    Message,
    ProviderRequest,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    message_from_dict,
    message_to_dict,
)

BASE = {"name": "base", "description": "Base", "input_schema": {"type": "object"}}
NEW = {"name": "new", "description": "New", "input_schema": {"type": "object"}}


class Capture:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)

        async def stream():
            if False:
                yield None

        return stream()


def change(*, added=(), removed=(), definitions=()):
    return CatalogChangeBlock(
        text="catalog changed",
        added_tools=added,
        removed_tools=removed,
        tool_definitions=definitions,
    )


def request(messages, *, model="claude-opus-5", tools=None, choice=None):
    return ProviderRequest(
        model=model,
        system=[],
        tools=[BASE] if tools is None else tools,
        messages=messages,
        tool_choice=choice,
    )


async def capture_anthropic(req, *, enabled=True, **options):
    from linch.providers.anthropic import AnthropicProvider, AnthropicProviderOptions

    transport = Capture()
    provider = AnthropicProvider(
        AnthropicProviderOptions(native_catalog_changes=enabled, **options)
    )
    provider._get_client = AsyncMock(return_value=SimpleNamespace(messages=transport))
    _ = [event async for event in provider.stream(req)]
    return transport.calls[0]


async def capture_responses(req, *, enabled=True):
    from linch.providers.openai_responses import (
        OpenAIResponsesProvider,
        OpenAIResponsesProviderOptions,
    )

    transport = Capture()
    provider = OpenAIResponsesProvider(
        OpenAIResponsesProviderOptions(native_catalog_changes=enabled)
    )
    provider._client._get_client = AsyncMock(return_value=SimpleNamespace(responses=transport))
    _ = [event async for event in provider.stream(req)]
    return transport.calls[0]


@pytest.mark.parametrize("provider", ["anthropic", "responses"])
async def test_option_off_phase2_bytes(provider):
    # `definitions` present or not makes no difference to the expected bytes
    # below — disabling the feature degrades the block to plain text either
    # way, so the stronger input (definitions present) is the one kept.
    block = change(added=("new",), definitions=(NEW,))
    req = request(
        [Message(role="user", content=[block])],
        model="claude-opus-5" if provider == "anthropic" else "gpt-5.4",
    )
    if provider == "anthropic":
        payload = await capture_anthropic(req, enabled=False)
        expected = {
            "model": "claude-opus-5",
            "max_tokens": 8096,
            "stream": True,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "catalog changed"}]}
            ],
            "tools": [BASE],
        }
    else:
        payload = await capture_responses(req, enabled=False)
        expected = {
            "model": "gpt-5.4",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "catalog changed"}],
                }
            ],
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "base",
                    "description": "Base",
                    "parameters": {"type": "object"},
                }
            ],
        }
    assert (
        json.dumps(payload, separators=(",", ":")).encode()
        == json.dumps(expected, separators=(",", ":")).encode()
    )


@pytest.mark.parametrize(
    "model,options,expected",
    [
        ("claude-opus-5", {}, "positional"),
        ("claude-opus-4-8", {}, "positional"),
        ("claude-sonnet-5", {}, "append_only"),
        ("claude-opus-5-unknown", {}, "append_only"),
        ("claude-opus-5", {"base_url": "https://proxy.example"}, "append_only"),
        ("claude-opus-5", {"api_mode": "compatible"}, "append_only"),
        (
            "claude-opus-5",
            {"base_url": "https://proxy.example", "api_mode": "native"},
            "append_only",
        ),
        ("claude-opus-5", {"base_url": "https://bedrock.us-east-1.amazonaws.com"}, "append_only"),
        (
            "claude-opus-5",
            {"base_url": "https://us-east1-aiplatform.googleapis.com"},
            "append_only",
        ),
        ("claude-opus-5", {"base_url": "https://api.anthropic.com"}, "positional"),
        ("claude-fable-5-1", {}, "positional"),
        ("claude-mythos-5-1", {}, "positional"),
        ("claude-fable-5", {}, "positional"),
        ("claude-mythos-5", {}, "positional"),
        ("claude-opus-5-5", {}, "positional"),
        ("claude-sonnet-5-5", {}, "positional"),
        ("claude-haiku-5-5", {}, "positional"),
    ],
)
def test_anthropic_gating(model, options, expected):
    from linch.providers.anthropic import AnthropicProvider, AnthropicProviderOptions

    assert (
        AnthropicProvider(AnthropicProviderOptions(native_catalog_changes=True, **options))
        .capabilities(model)
        .catalog_changes
        == expected
    )


@pytest.mark.parametrize("base_url", [None, "https://api.anthropic.com"])
async def test_anthropic_environment_endpoint_gates_native_catalog(monkeypatch, base_url):
    from linch.providers.anthropic import AnthropicProvider, AnthropicProviderOptions

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.invalid")
    options = AnthropicProviderOptions(native_catalog_changes=True, base_url=base_url)
    expected = "append_only" if base_url is None else "positional"
    assert AnthropicProvider(options).capabilities("claude-opus-5").catalog_changes == expected
    wire = await capture_anthropic(
        request([Message(role="user", content=[change(added=("new",), definitions=(NEW,))])]),
        base_url=base_url,
    )
    assert any(message["role"] == "system" for message in wire["messages"]) == bool(base_url)
    assert (
        "inline-tools-2026-09-15" in wire.get("extra_headers", {}).get("anthropic-beta", "")
    ) == bool(base_url)


@pytest.mark.parametrize(
    "model,expected",
    [
        ("gpt-5.3", "append_only"),
        ("gpt-5.4", "positional"),
        ("gpt-5.5", "positional"),
        ("gpt-5.10", "positional"),
        ("gpt-6", "positional"),
        ("unknown", "append_only"),
    ],
)
def test_responses_gating(model, expected):
    from linch.providers.openai_responses import (
        OpenAIResponsesProvider,
        OpenAIResponsesProviderOptions,
    )

    assert (
        OpenAIResponsesProvider(OpenAIResponsesProviderOptions(native_catalog_changes=True))
        .capabilities(model)
        .catalog_changes
        == expected
    )


async def test_anthropic_addition_position_and_prefix():
    prior = [
        Message(role="user", content=[TextBlock("hello")]),
        Message(role="assistant", content=[TextBlock("answer")]),
    ]
    notice = Message(role="user", content=[change(added=("new",), definitions=(NEW,))])
    before = await capture_anthropic(request(prior))
    after = await capture_anthropic(
        request(prior + [notice, Message(role="assistant", content=[TextBlock("next")])])
    )
    assert after["tools"] == [BASE]
    assert json.dumps(after["messages"][:2]).encode() == json.dumps(before["messages"]).encode()
    assert after["messages"][2:] == [
        {"role": "user", "content": [{"type": "text", "text": "catalog changed"}]},
        {
            "role": "system",
            "content": [
                {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": NEW}}
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "next"}]},
    ]


async def test_anthropic_parallel_results_before_system():
    msgs = [
        Message(role="user", content=[TextBlock("go")]),
        Message(
            role="assistant", content=[ToolUseBlock("a", "base", {}), ToolUseBlock("b", "base", {})]
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock("a", "one"),
                ToolResultBlock("b", "two"),
                change(added=("new",), definitions=(NEW,)),
            ],
        ),
    ]
    wire = (await capture_anthropic(request(msgs)))["messages"]
    assert wire[-2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "one"},
            {"type": "tool_result", "tool_use_id": "b", "content": "two"},
            {"type": "text", "text": "catalog changed"},
        ],
    }
    assert wire[-1] == {
        "role": "system",
        "content": [
            {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": NEW}}
        ],
    }


async def test_anthropic_abort_then_prompt_buffers_system():
    msgs = [
        Message(role="user", content=[TextBlock("go")]),
        Message(role="user", content=[change(added=("new",), definitions=(NEW,))]),
        Message(role="user", content=[TextBlock("retry")]),
    ]
    wire = (await capture_anthropic(request(msgs)))["messages"]
    assert wire[-2]["role"] == "user"
    assert wire[-2]["content"][-1] == {"type": "text", "text": "retry"}
    assert wire[-1] == {
        "role": "system",
        "content": [
            {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": NEW}}
        ],
    }


async def test_anthropic_prefix_removal_and_restore_reference():
    msgs = [
        Message(role="user", content=[TextBlock("go"), change(removed=("base",))]),
        Message(role="assistant", content=[TextBlock("ok")]),
        Message(role="user", content=[change(added=("base",))]),
    ]
    wire = (await capture_anthropic(request(msgs)))["messages"]
    assert wire[1] == {
        "role": "system",
        "content": [{"type": "tool_removal", "tool": {"type": "tool_reference", "name": "base"}}],
    }
    assert wire[-1] == {
        "role": "system",
        "content": [{"type": "tool_addition", "tool": {"type": "tool_reference", "name": "base"}}],
    }


async def test_anthropic_duplicate_addition_after_compaction_skipped():
    msgs = [
        Message(role="user", content=[TextBlock("go"), change(added=("new",), definitions=(NEW,))]),
        Message(role="assistant", content=[TextBlock("ok")]),
        Message(role="user", content=[change(added=("new",), definitions=(NEW,))]),
    ]
    wire = (await capture_anthropic(request(msgs)))["messages"]
    assert sum(m["role"] == "system" for m in wire) == 1


async def test_anthropic_definition_limit():
    from linch.errors import ConfigError

    huge = {**NEW, "description": "x" * 4_194_305}
    with pytest.raises(ConfigError, match="(?i)(tool|4|limit)"):
        await capture_anthropic(
            request([Message(role="user", content=[change(added=("new",), definitions=(huge,))])])
        )


async def test_anthropic_definition_limit_counts_utf8_bytes():
    from linch.errors import ConfigError

    # Under 4 MiB in characters, over it as an actual UTF-8 tool definition.
    huge = {**NEW, "description": "界" * 1_400_000}
    assert len(json.dumps(huge, ensure_ascii=False)) < 4_194_304
    with pytest.raises(ConfigError, match="(?i)(tool|4|limit)"):
        await capture_anthropic(
            request([Message(role="user", content=[change(added=("new",), definitions=(huge,))])])
        )


async def test_anthropic_beta_on_every_request():
    for msgs in (
        [Message(role="user", content=[TextBlock("hi")])],
        [Message(role="user", content=[change(added=("new",), definitions=(NEW,))])],
    ):
        wire = await capture_anthropic(request(msgs))
        assert "inline-tools-2026-09-15" in wire["extra_headers"]["anthropic-beta"]


async def test_responses_roundtrip_and_previous_response_position():
    notice = Message(role="user", content=[change(added=("new",), definitions=(NEW,))])
    notice = message_from_dict(message_to_dict(notice))
    msgs = [
        Message(
            role="assistant",
            content=[TextBlock("prior")],
            provider_metadata={"openai_responses": {"response_id": "resp_old"}},
        ),
        notice,
        Message(role="assistant", content=[TextBlock("later")]),
    ]
    wire = await capture_responses(request(msgs, model="gpt-5.4"))
    assert wire["previous_response_id"] == "resp_old"
    assert wire["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "catalog changed"}],
        },
        {
            "type": "additional_tools",
            "role": "developer",
            "tools": [
                {
                    "type": "function",
                    "name": "new",
                    "description": "New",
                    "parameters": {"type": "object"},
                }
            ],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "later"}],
        },
    ]
    assert wire["tool_choice"] == {
        "type": "allowed_tools",
        "mode": "auto",
        "tools": [{"type": "function", "name": "base"}, {"type": "function", "name": "new"}],
    }


@pytest.mark.parametrize(
    "choice,expected",
    [
        (None, {"type": "allowed_tools", "mode": "auto", "tools": []}),
        ("auto", {"type": "allowed_tools", "mode": "auto", "tools": []}),
        ("none", "none"),
        ({"type": "tool", "name": "base"}, {"type": "function", "name": "base"}),
    ],
)
async def test_responses_removal_and_forced_choice(choice, expected):
    wire = await capture_responses(
        request(
            [Message(role="user", content=[change(removed=("base",))])],
            model="gpt-5.4",
            choice=choice,
        )
    )
    assert wire["tool_choice"] == expected


@pytest.mark.parametrize("provider", ["anthropic", "responses"])
async def test_phase2_frozen_fixture_without_new_options(provider):
    """Validate the literal option-off oracle against the untouched phase-2 adapters."""
    req = request(
        [Message(role="user", content=[CatalogChangeBlock(text="catalog changed")])],
        model="claude-opus-5" if provider == "anthropic" else "gpt-5.4",
    )
    transport = Capture()
    if provider == "anthropic":
        from linch.providers.anthropic import AnthropicProvider

        adapter = AnthropicProvider()
        adapter._get_client = AsyncMock(return_value=SimpleNamespace(messages=transport))
        expected = (
            b'{"model":"claude-opus-5","max_tokens":8096,"stream":true,"messages":[{"role":"'
            b'user","content":[{"type":"text","text":"catalog changed"}]}],"tools":[{"name":'
            b'"base","description":"Base","input_schema":{"type":"object"}}]}'
        )
    else:
        from linch.providers.openai_responses import OpenAIResponsesProvider

        adapter = OpenAIResponsesProvider()
        adapter._client._get_client = AsyncMock(return_value=SimpleNamespace(responses=transport))
        expected = (
            b'{"model":"gpt-5.4","input":[{"type":"message","role":"user","content":[{"type"'
            b':"input_text","text":"catalog changed"}]}],"stream":true,"tools":[{"type":"fun'
            b'ction","name":"base","description":"Base","parameters":{"type":"object"}}]}'
        )
    _ = [event async for event in adapter.stream(req)]
    assert json.dumps(transport.calls[0], separators=(",", ":")).encode() == expected


async def test_anthropic_by_value_removal_and_restore_do_not_reference_it():
    wire = (
        await capture_anthropic(
            request(
                [
                    Message(role="user", content=[change(added=("new",), definitions=(NEW,))]),
                    Message(role="assistant", content=[TextBlock("added")]),
                    Message(role="user", content=[change(removed=("new",))]),
                    Message(role="assistant", content=[TextBlock("removed")]),
                    Message(role="user", content=[change(added=("new",), definitions=(NEW,))]),
                ]
            )
        )
    )["messages"]
    assert [message for message in wire if message["role"] == "system"] == [
        {
            "role": "system",
            "content": [
                {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": NEW}}
            ],
        }
    ]
    assert sum(message["role"] == "user" for message in wire) == 3


async def test_anthropic_native_definition_has_only_translated_tool_fields():
    definition = {**NEW, "private_extra": "must not travel", "cache_control": {"type": "ephemeral"}}
    wire = await capture_anthropic(
        request([Message(role="user", content=[change(added=("new",), definitions=(definition,))])])
    )
    assert wire["messages"][-1] == {
        "role": "system",
        "content": [
            {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": NEW}}
        ],
    }


async def test_anthropic_redundant_removal_is_skipped():
    wire = (
        await capture_anthropic(
            request(
                [
                    Message(role="user", content=[change(removed=("base", "missing"))]),
                    Message(role="assistant", content=[TextBlock("ok")]),
                    Message(role="user", content=[change(removed=("base",))]),
                ]
            )
        )
    )["messages"]
    assert [message for message in wire if message["role"] == "system"] == [
        {
            "role": "system",
            "content": [
                {"type": "tool_removal", "tool": {"type": "tool_reference", "name": "base"}}
            ],
        }
    ]


async def test_anthropic_schema_promoted_definition_is_not_redeclared():
    edited = {**NEW, "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}
    wire = await capture_anthropic(
        request(
            [
                Message(role="user", content=[change(added=("new",), definitions=(NEW,))]),
                Message(role="assistant", content=[TextBlock("ok")]),
                Message(role="user", content=[TextBlock("schema changed")]),
            ],
            tools=[BASE, edited],
        )
    )
    assert wire["tools"] == [BASE, edited]
    assert all(message["role"] != "system" for message in wire["messages"])


async def test_anthropic_cache_breakpoint_stays_on_user_text():
    req = request([Message(role="user", content=[change(added=("new",), definitions=(NEW,))])])
    req.cache_prompt = True
    req.cache_ttl = "1h"
    wire = await capture_anthropic(req)
    assert wire["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "catalog changed",
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ],
        },
        {
            "role": "system",
            "content": [
                {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": NEW}}
            ],
        },
    ]


async def test_anthropic_definition_limit_is_aggregate_and_pretransport():
    from linch.errors import ConfigError

    definitions = tuple(
        {**NEW, "name": name, "description": "x" * 2_100_000} for name in ("one", "two")
    )
    from linch.providers.anthropic import AnthropicProvider, AnthropicProviderOptions

    transport = Capture()
    adapter = AnthropicProvider(AnthropicProviderOptions(native_catalog_changes=True))
    adapter._get_client = AsyncMock(return_value=SimpleNamespace(messages=transport))
    with pytest.raises(ConfigError, match="(?i)(tool|4|limit)"):
        _ = [
            event
            async for event in adapter.stream(
                request(
                    [
                        Message(
                            role="user",
                            content=[change(added=("one", "two"), definitions=definitions)],
                        )
                    ]
                )
            )
        ]
    assert transport.calls == []


@pytest.mark.parametrize("header", ["anthropic-beta", "Anthropic-Beta"])
async def test_anthropic_beta_merges_existing_header(header):
    wire = await capture_anthropic(
        request([Message(role="user", content=[TextBlock("hi")])]),
        default_headers={header: "existing-beta"},
    )
    assert set(wire["extra_headers"]["anthropic-beta"].split(",")) == {
        "existing-beta",
        "inline-tools-2026-09-15",
    }


async def test_responses_roundtrip_without_previous_response_retains_item_position():
    notice = message_from_dict(
        message_to_dict(
            Message(
                role="user",
                content=[
                    TextBlock("before"),
                    change(added=("new",), definitions=(NEW,)),
                    TextBlock("after"),
                ],
            )
        )
    )
    wire = await capture_responses(request([notice], model="gpt-5.4"))
    assert wire["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "before"},
                {"type": "input_text", "text": "catalog changed"},
            ],
        },
        {
            "type": "additional_tools",
            "role": "developer",
            "tools": [
                {
                    "type": "function",
                    "name": "new",
                    "description": "New",
                    "parameters": {"type": "object"},
                }
            ],
        },
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "after"}]},
    ]


async def test_responses_active_names_include_history_before_previous_response():
    wire = await capture_responses(
        request(
            [
                Message(role="user", content=[change(added=("new",), definitions=(NEW,))]),
                Message(
                    role="assistant",
                    content=[TextBlock("prior")],
                    provider_metadata={"openai_responses": {"response_id": "resp_old"}},
                ),
                Message(role="user", content=[change(removed=("base",)), TextBlock("next")]),
            ],
            model="gpt-5.4",
        )
    )
    assert wire["previous_response_id"] == "resp_old"
    assert all(item["type"] != "additional_tools" for item in wire["input"])
    assert wire["tool_choice"] == {
        "type": "allowed_tools",
        "mode": "auto",
        "tools": [{"type": "function", "name": "new"}],
    }


async def test_responses_schema_promoted_definition_is_not_redeclared():
    edited = {**NEW, "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}
    wire = await capture_responses(
        request(
            [
                Message(role="user", content=[change(added=("new",), definitions=(NEW,))]),
                Message(role="assistant", content=[TextBlock("ok")]),
                Message(role="user", content=[TextBlock("schema changed")]),
            ],
            tools=[BASE, edited],
            model="gpt-5.4",
        )
    )
    assert wire["tools"][1]["parameters"] == edited["input_schema"]
    assert all(item["type"] != "additional_tools" for item in wire["input"])


@pytest.mark.parametrize(
    "provider,model,options",
    [
        ("anthropic", "claude-sonnet-5", {}),
        ("anthropic", "unknown", {}),
        ("anthropic", "claude-opus-5", {"base_url": "https://proxy.example", "api_mode": "native"}),
        ("responses", "gpt-5.3", {}),
        ("responses", "unknown", {}),
    ],
)
async def test_unsupported_model_or_endpoint_keeps_phase2_wire(provider, model, options):
    req = request(
        [Message(role="user", content=[change(added=("new",), definitions=(NEW,))])], model=model
    )
    if provider == "anthropic":
        wire = await capture_anthropic(req, **options)
        assert wire["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "catalog changed"}]}
        ]
        assert "inline-tools-2026-09-15" not in wire.get("extra_headers", {}).get(
            "anthropic-beta", ""
        )
    else:
        wire = await capture_responses(req)
        assert wire["input"] == [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "catalog changed"}],
            }
        ]
        assert "tool_choice" not in wire


def test_provider_capabilities_default_to_append_only():
    from linch.providers.base import ProviderCapabilities, full_capabilities

    assert ProviderCapabilities().catalog_changes == "append_only"
    assert full_capabilities(128_000).catalog_changes == "append_only"
