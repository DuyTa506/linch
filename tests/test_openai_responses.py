from agent_kit.openai_responses import OpenAIReasoning, build_payload, map_wire_events
from agent_kit.types import Message, ProviderRequest, SystemBlock, TextBlock, ToolResultBlock, Usage


def test_build_payload_translates_responses_shape() -> None:
    req = ProviderRequest(
        model="gpt-5",
        system=[SystemBlock(text="system")],
        tools=[
            {
                "name": "Read",
                "description": "Read file",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        messages=[
            Message(role="user", content=[TextBlock(text="hello")]),
            Message(role="user", content=[ToolResultBlock(tool_use_id="call_1", content="ok")]),
        ],
    )

    payload = build_payload(req, OpenAIReasoning(effort="medium", summary="auto"))

    assert payload["model"] == "gpt-5"
    assert payload["instructions"] == "system"
    assert payload["tools"][0]["type"] == "function"
    assert payload["input"][0]["content"][0]["text"] == "hello"
    assert payload["input"][1]["type"] == "function_call_output"
    assert payload["reasoning"] == {"effort": "medium", "summary": "auto"}


async def test_map_wire_events_tool_call() -> None:
    async def wire():
        yield {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "item_1",
                "call_id": "call_1",
                "name": "Read",
            },
        }
        yield {
            "type": "response.function_call_arguments.delta",
            "item_id": "item_1",
            "delta": '{"path":"README.md"}',
        }
        yield {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "id": "item_1"},
        }
        yield {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "usage": {"input_tokens": 1, "output_tokens": 2},
                "output": [{"type": "function_call"}],
            },
        }

    events = [event async for event in map_wire_events(wire(), "gpt-5")]

    assert events[0] == {"type": "message_start", "model": "gpt-5"}
    assert events[1] == {"type": "tool_use_start", "id": "call_1", "name": "Read"}
    assert events[2]["type"] == "tool_use_input_delta"
    assert events[3] == {"type": "tool_use_end", "id": "call_1"}
    assert events[-1]["stop_reason"] == "tool_use"
    assert isinstance(events[-1]["usage"], Usage)
