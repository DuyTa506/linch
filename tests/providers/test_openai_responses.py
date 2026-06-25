from linch.openai_responses import OpenAIReasoning, build_payload, map_wire_events
from linch.types import Message, ProviderRequest, SystemBlock, TextBlock, ToolResultBlock, Usage


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


def test_map_openai_error_retry_after_numeric_and_non_context_400() -> None:
    from linch.errors import ProviderError, RateLimitError
    from linch.openai_responses import map_openai_error

    class Resp:
        headers = {"retry-after": "3"}

    class RateLimited(Exception):
        status_code = 429
        response = Resp()

    rate = map_openai_error(RateLimited("rate limited"))
    assert isinstance(rate, RateLimitError)
    assert rate.retry_after_seconds == 3

    class BadReq(Exception):
        status_code = 400

    bad = map_openai_error(BadReq("context field is malformed"))
    assert isinstance(bad, ProviderError)


def test_map_openai_error_structured_context_length() -> None:
    from linch.errors import ContextLengthError
    from linch.openai_responses import map_openai_error

    class BadReq(Exception):
        status_code = 400
        body = {"error": {"code": "context_length_exceeded", "message": "too many tokens"}}

    assert isinstance(map_openai_error(BadReq("bad request")), ContextLengthError)


def test_map_openai_error_context_length_without_status_is_not_retried() -> None:
    """A context-overflow from a status-less endpoint must not become a retry storm.

    OpenAI-compatible / local servers (DeepSeek, llama.cpp) may raise a
    context-overflow with no integer status_code. Gating context-length detection
    on ``status == 400`` then misclassified it as a retryable ProviderError.
    """
    from linch.errors import ContextLengthError, ProviderError
    from linch.openai_responses import map_openai_error

    class NoStatusStructured(Exception):
        body = {"error": {"code": "context_length_exceeded", "message": "too long"}}

    mapped = map_openai_error(NoStatusStructured("boom"))
    assert isinstance(mapped, ContextLengthError)
    assert not isinstance(mapped, ProviderError)
    assert mapped.retryable is False

    class NoStatusMessage(Exception):
        pass

    mapped2 = map_openai_error(NoStatusMessage("prompt is too long: 201537 tokens"))
    assert isinstance(mapped2, ContextLengthError)
    assert mapped2.retryable is False


def test_build_usage_surfaces_cache_read_tokens() -> None:
    """Responses API cache hits must surface as Usage.cache_read_tokens
    (the observable proof the prompt cache is working)."""
    from linch.openai_responses import build_usage

    raw = {
        "input_tokens": 1000,
        "output_tokens": 50,
        "input_tokens_details": {"cached_tokens": 768},
    }
    usage = build_usage(raw)
    assert usage.input_tokens == 1000
    assert usage.cache_read_tokens == 768

    # No cache details / no usage → 0, never raises.
    assert build_usage({"input_tokens": 10, "output_tokens": 2}).cache_read_tokens == 0
    assert build_usage(None).cache_read_tokens == 0
