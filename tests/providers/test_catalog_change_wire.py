from __future__ import annotations

from linch.types import (
    CatalogChangeBlock,
    Message,
    ProviderRequest,
    SystemBlock,
    message_from_dict,
    message_to_dict,
)


def _change_message() -> Message:
    return Message(
        role="user",
        content=[
            CatalogChangeBlock(
                text="<system-reminder>Tool catalog changed: added `new_tool`.</system-reminder>",
                added_tools=("new_tool",),
            )
        ],
    )


def test_catalog_change_block_round_trips_through_message_wire() -> None:
    message = _change_message()

    restored = message_from_dict(message_to_dict(message))

    assert restored == message


def test_provider_adapters_render_catalog_change_block_as_text() -> None:
    message = _change_message()

    from linch.openai_responses import translate_input
    from linch.providers.anthropic import _translate_messages as anthropic_messages
    from linch.providers.gemini import _translate_messages as gemini_messages
    from linch.providers.openai_chat import _build_chat_payload

    request = ProviderRequest(
        model="model",
        system=[SystemBlock(text="system")],
        tools=[],
        messages=[message],
    )

    assert translate_input([message])[0]["content"][0]["text"].startswith("<system-reminder>")
    assert _build_chat_payload(request)["messages"][1]["content"][0]["text"].startswith(
        "<system-reminder>"
    )
    assert anthropic_messages([message])[0]["content"][0]["text"].startswith("<system-reminder>")
    assert gemini_messages([message])[0]["parts"][0]["text"].startswith("<system-reminder>")


def test_context_token_estimator_counts_catalog_change_text() -> None:
    from linch.context.builder import _estimate_message

    message = _change_message()

    assert _estimate_message(message) == len(message.content[0].text) // 4
