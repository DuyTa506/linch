"""Structured-output plumbing through subagent runner."""

from __future__ import annotations


async def test_run_subagent_forwards_output_schema_run_options() -> None:
    from linch import Agent, OutputSchema, RunOptions
    from linch.evals import ScriptedProvider, TextTurn
    from linch.sessions import InMemorySessionStore
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    schema = OutputSchema(
        name="answer",
        schema={
            "type": "object",
            "properties": {"answer": {"type": "integer"}},
            "required": ["answer"],
        },
    )
    agent = Agent(
        model="gpt-5",
        provider=ScriptedProvider([TextTurn('{"answer":42}')]),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
    )
    parent = await agent.session()

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="answer",
            display_name="helper",
            subagent_run_id="sa_structured",
            run_options=RunOptions(output_schema=schema),
        )
    )

    assert not result.errored
    assert result.structured_output == {"answer": 42}
    assert result.structured_error is None


async def test_run_subagent_forwards_final_tool_name_run_options() -> None:
    from linch import Agent, RunOptions
    from linch.evals import ScriptedProvider, ToolUseTurn
    from linch.sessions import InMemorySessionStore
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, result_text_for_caller, run_subagent

    agent = Agent(
        model="gpt-5",
        provider=ScriptedProvider([ToolUseTurn("emit_answer", {"answer": 42})]),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
    )
    parent = await agent.session()

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="answer",
            display_name="helper",
            subagent_run_id="sa_final_tool",
            run_options=RunOptions(final_tool_name="emit_answer"),
        )
    )

    assert not result.errored
    assert result.final_text == ""
    assert result.structured_output == {"answer": 42}
    assert result_text_for_caller(result) == '{"answer":42}'
