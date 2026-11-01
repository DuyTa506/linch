from __future__ import annotations

from typing import Any

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn


async def _run(provider: Any, options: Any, run_id: str) -> Any:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    agent = make_agent(provider, model="gpt-5")
    parent = await agent.session()
    return await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="answer",
            display_name="helper",
            subagent_run_id=run_id,
            run_options=options,
        )
    )


async def test_run_subagent_forwards_output_options() -> None:
    from linch import OutputSchema, RunOptions
    from linch.subagents.runner import result_text_for_caller

    schema = OutputSchema(
        name="answer",
        schema={
            "type": "object",
            "properties": {"answer": {"type": "integer"}},
            "required": ["answer"],
        },
    )
    result = await _run(
        ScriptedProvider([TextTurn('{"answer":42}')]),
        RunOptions(output_schema=schema),
        "sa_structured",
    )
    assert not result.errored
    assert result.structured_output == {"answer": 42}
    assert result.structured_error is None
    final = await _run(
        ScriptedProvider([ToolUseTurn("emit_answer", {"answer": 42})]),
        RunOptions(final_tool_name="emit_answer"),
        "sa_final_tool",
    )
    assert not final.errored
    assert final.final_text == ""
    assert final.structured_output == {"answer": 42}
    assert result_text_for_caller(final) == '{"answer":42}'
