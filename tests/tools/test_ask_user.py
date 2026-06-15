from __future__ import annotations

import asyncio

import pytest


def _input(**overrides):
    data = {
        "questions": [
            {
                "id": "mode",
                "header": "Mode",
                "question": "Choose a mode.",
                "options": [
                    {"label": "Fast", "description": "Prioritize speed."},
                    {"label": "Careful", "description": "Prioritize checks."},
                ],
            }
        ]
    }
    data.update(overrides)
    return data


def _ctx(signal=None):
    from linch.tools import ToolContext

    return ToolContext(cwd=".", session_id="s", run_id="r", session_store=None, signal=signal)


@pytest.mark.asyncio
async def test_ask_user_validates_shape() -> None:
    from linch.tools import AskUserResponse, AskUserTool

    tool = AskUserTool(lambda request, ctx: AskUserResponse(accepted=True))

    with pytest.raises(ValueError, match="1-4 questions"):
        tool.validate({"questions": []})
    with pytest.raises(ValueError, match="12 characters"):
        tool.validate(
            _input(
                questions=[
                    {
                        "id": "x",
                        "header": "TooLongHeader",
                        "question": "Pick.",
                        "options": [
                            {"label": "A", "description": "A."},
                            {"label": "B", "description": "B."},
                        ],
                    }
                ]
            )
        )
    with pytest.raises(ValueError, match="unique"):
        tool.validate(
            _input(
                questions=[
                    {
                        "id": "x",
                        "header": "Pick",
                        "question": "Pick.",
                        "options": [
                            {"label": "A", "description": "A."},
                            {"label": "A", "description": "B."},
                        ],
                    }
                ]
            )
        )


@pytest.mark.asyncio
async def test_ask_user_returns_handler_answers() -> None:
    from linch.tools import AskUserResponse, AskUserTool

    async def handler(request, ctx):
        assert request.questions[0].id == "mode"
        return AskUserResponse(accepted=True, answers={"mode": "Fast"})

    tool = AskUserTool(handler)
    result = await tool.execute(tool.validate(_input()), _ctx())

    assert result.is_error is False
    assert '"mode": "Fast"' in result.content


@pytest.mark.asyncio
async def test_ask_user_decline_is_non_error_assumption_instruction() -> None:
    from linch.tools import AskUserResponse, AskUserTool

    tool = AskUserTool(lambda request, ctx: AskUserResponse(accepted=False, note="you decide"))
    result = await tool.execute(tool.validate(_input()), _ctx())

    assert result.is_error is False
    assert "Proceed with explicit assumptions" in result.content
    assert "you decide" in result.content


@pytest.mark.asyncio
async def test_ask_user_propagates_handler_errors() -> None:
    from linch.tools import AskUserTool

    def handler(request, ctx):
        raise RuntimeError("handler failed")

    tool = AskUserTool(handler)
    with pytest.raises(RuntimeError, match="handler failed"):
        await tool.execute(tool.validate(_input()), _ctx())


@pytest.mark.asyncio
async def test_ask_user_aborts_while_handler_waits() -> None:
    from linch.abort import AbortContext
    from linch.errors import AbortError
    from linch.tools import AskUserTool

    async def handler(request, ctx):
        await asyncio.sleep(10)
        return {"accepted": True}

    signal = AbortContext()
    tool = AskUserTool(handler)
    task = asyncio.create_task(tool.execute(tool.validate(_input()), _ctx(signal)))
    await asyncio.sleep(0)
    signal.abort()
    with pytest.raises(AbortError):
        await task


@pytest.mark.asyncio
async def test_ask_user_malformed_response_fails_closed() -> None:
    """A handler that returns no acceptance signal must NOT be read as consent."""
    from linch.tools import AskUserTool

    for value in (None, {}, "garbage", 123, {"unexpected": True}):
        tool = AskUserTool(lambda request, ctx, _v=value: _v)
        result = await tool.execute(tool.validate(_input()), _ctx())
        assert result.summary == "User declined AskUser", value


@pytest.mark.asyncio
async def test_ask_user_answers_without_accepted_flag_count_as_accepted() -> None:
    """A handler that returns answers but omits `accepted` is treated as accepted."""
    import json

    from linch.tools import AskUserTool

    tool = AskUserTool(lambda request, ctx: {"answers": {"mode": "Fast"}})
    result = await tool.execute(tool.validate(_input()), _ctx())
    assert result.summary == "User answered AskUser"
    assert json.loads(result.content)["answers"] == {"mode": "Fast"}


@pytest.mark.asyncio
async def test_ask_user_sync_handler_runs_off_the_event_loop() -> None:
    """A blocking synchronous handler must run on a worker thread, not the loop."""
    import threading

    from linch.tools import AskUserTool

    loop_thread = threading.get_ident()
    captured: dict[str, int] = {}

    def handler(request, ctx):
        captured["thread"] = threading.get_ident()
        return {"accepted": True, "answers": {"mode": "Fast"}}

    tool = AskUserTool(handler)
    await tool.execute(tool.validate(_input()), _ctx())
    assert captured["thread"] != loop_thread


@pytest.mark.asyncio
async def test_ask_user_times_out_to_declined() -> None:
    """With a timeout configured, an unanswered prompt declines instead of hanging."""
    from linch.tools import AskUserTool

    async def handler(request, ctx):
        await asyncio.sleep(10)
        return {"accepted": True}

    tool = AskUserTool(handler, timeout_s=0.05)
    result = await tool.execute(tool.validate(_input()), _ctx())
    assert result.summary == "User declined AskUser"
