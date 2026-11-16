"""Each optional `AgentConfig` field, end to end against the real server."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from scripted import MODEL, config, text_turn, tool_turn

from linch_rust import (
    AgentConfig,
    BudgetEvent,
    Envelope,
    ErrorEvent,
    InitEvent,
    ModelFallbackEvent,
    OpenError,
    Profile,
    ProviderRetry,
    ProviderRetryEvent,
    ResultEvent,
    Server,
    ToolCallEndEvent,
    UsageEvent,
    UserEvent,
    tool,
)

TASKS = ["TaskCreate", "TaskList", "TaskGet", "TaskUpdate"]
CONFIG_RS = Path(__file__).resolve().parents[2] / "crates/linch/src/config.rs"
RELIABILITY_RS = Path(__file__).resolve().parents[2] / "crates/linch-core/src/reliability.rs"


async def run_once(cfg: AgentConfig, session_id: str = "s1") -> list[Envelope]:
    async with Server() as server:
        agent = await server.open_agent(cfg)
        run = await agent.run(session_id, "go")
        return [envelope async for envelope in run]


def tools_of(envelopes: list[Envelope]) -> list[str]:
    [init] = [e.event for e in envelopes if isinstance(e.event, InitEvent)]
    return init.tools


def results(envelopes: list[Envelope]) -> list[str]:
    ends = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
    return ["".join(part.get("text", "") for part in end.result["content"]) for end in ends]


def final(envelopes: list[Envelope]) -> ResultEvent:
    last = envelopes[-1].event
    assert isinstance(last, ResultEvent), last
    return last


@pytest.mark.source
def test_the_typed_config_names_every_field_the_server_reads() -> None:
    source = CONFIG_RS.read_text(encoding="utf-8")
    body = source.split("pub struct AgentConfig {", 1)[1].split("\n}", 1)[0]
    fields = set(re.findall(r"^\s*pub (\w+):", body, re.MULTILINE))
    assert fields == AgentConfig.__required_keys__ | AgentConfig.__optional_keys__
    assert AgentConfig.__required_keys__ == {"namespace", "spec", "provider"}


async def test_the_task_tools_are_off_unless_tasks_is_on() -> None:
    assert tools_of(await run_once(config([text_turn("hi")]))) == []
    assert tools_of(await run_once(config([text_turn("hi")], tasks=True))) == TASKS


async def test_the_task_tools_track_work_without_asking() -> None:
    turns = [
        tool_turn("c1", "TaskCreate", {"subject": "Write docs", "description": "All"}),
        tool_turn("c2", "TaskUpdate", {"id": "1", "status": "completed"}),
        tool_turn("c3", "TaskList", {}),
        text_turn("done"),
    ]
    envelopes = await run_once(config(turns, mode="default", tasks=True))

    assert results(envelopes) == [
        "Task #1 created: Write docs",
        "Task #1 updated: [completed] Write docs",
        "#1 [completed] Write docs",
    ]
    assert final(envelopes).final_text == "done", "no approval was asked"


def subagent_call(call_id: str = "c1") -> dict[str, Any]:
    return tool_turn(call_id, "Subagent", {"description": "Check", "prompt": "Check it"})


async def test_subagents_run_a_child_on_the_same_provider(tmp_path: Path) -> None:
    turns = [subagent_call(), text_turn("child answer"), text_turn("done")]
    cfg = config(turns, mode="skip_dangerous", subagents=True, config_dir=str(tmp_path))
    envelopes = await run_once(cfg)

    assert tools_of(envelopes) == ["Subagent"]
    assert results(envelopes) == ["child answer"]
    assert final(envelopes).final_text == "done"


async def test_a_spec_budget_caps_the_run_and_its_children(tmp_path: Path) -> None:
    turns = [subagent_call(), text_turn("child answer"), text_turn("done")]
    cfg = config(turns, mode="skip_dangerous", subagents=True, config_dir=str(tmp_path))
    cfg["spec"] = {"model": MODEL, "budget": {"max_tokens": 8}}
    envelopes = await run_once(cfg)

    # The parent's call and its child's spent 4 tokens each: no third call.
    assert results(envelopes) == ["child answer"]
    [exceeded] = [e.event for e in envelopes if isinstance(e.event, BudgetEvent)]
    assert (exceeded.kind, exceeded.spent_tokens, exceeded.max_tokens) == ("exceeded", 8, 8)
    [error] = [e.event for e in envelopes if isinstance(e.event, ErrorEvent)]
    assert error.error["name"] == "BudgetExceededError"
    assert (final(envelopes).subtype, final(envelopes).stop_reason) == ("error", "error")


def overloaded() -> dict[str, Any]:
    """A scripted call that fails with a retryable 529 before any event."""
    return {"error": {"type": "status", "status": 529, "message": "overloaded"}}


async def test_a_spec_retries_a_failed_call_then_fails_over() -> None:
    turns = [overloaded(), overloaded(), text_turn("done", model="fallback-model")]
    retry: ProviderRetry = {"max_attempts": 2, "base_delay_ms": 1, "jitter": 0.0}
    cfg = config(turns)
    cfg["spec"] = {"model": MODEL, "provider_retry": retry, "fallback_models": ["fallback-model"]}
    envelopes = await run_once(cfg)

    [retried] = [e.event for e in envelopes if isinstance(e.event, ProviderRetryEvent)]
    assert (retried.model, retried.retry, retried.delay_ms) == (MODEL, 1, 1)
    assert retried.error["status"] == 529 and retried.error["retryable"]
    [fallback] = [e.event for e in envelopes if isinstance(e.event, ModelFallbackEvent)]
    assert (fallback.from_model, fallback.to_model) == (MODEL, "fallback-model")
    [usage] = [e.event for e in envelopes if isinstance(e.event, UsageEvent)]
    assert usage.actual_model == "fallback-model"
    assert final(envelopes).final_text == "done"


async def test_a_capped_budget_retries_a_call_that_cost_nothing() -> None:
    refused = {"error": {"type": "connect", "message": "connection refused"}}
    retry: ProviderRetry = {"base_delay_ms": 1, "jitter": 0.0}
    cfg = config([refused, overloaded(), text_turn("done")])
    cfg["spec"] = {"model": MODEL, "provider_retry": retry, "budget": {"max_tokens": 100}}
    envelopes = await run_once(cfg)

    retried = [e.event for e in envelopes if isinstance(e.event, ProviderRetryEvent)]
    assert [r.error["kind"] for r in retried] == ["connect", "status"]
    assert final(envelopes).final_text == "done"
    assert final(envelopes).usage_complete


async def test_a_spec_without_a_retry_policy_ends_the_run_on_a_failure() -> None:
    envelopes = await run_once(config([overloaded(), text_turn("never")]))

    assert not [e for e in envelopes if isinstance(e.event, ProviderRetryEvent)]
    [error] = [e.event for e in envelopes if isinstance(e.event, ErrorEvent)]
    assert (error.error["status"], error.error["retryable"]) == (529, True)
    assert final(envelopes).subtype == "error"


async def test_a_retry_policy_that_cannot_run_fails_the_open() -> None:
    cfg = config([])
    cfg["spec"] = {"model": MODEL, "provider_retry": {"max_attempts": 0}}
    async with Server() as server:
        with pytest.raises(OpenError):
            await server.open_agent(cfg)


@pytest.mark.source
def test_the_typed_retry_policy_names_every_field_the_server_reads() -> None:
    source = RELIABILITY_RS.read_text(encoding="utf-8")
    body = source.split("pub struct ProviderRetry {", 1)[1].split("\n}", 1)[0]
    fields = set(re.findall(r"^\s*pub (\w+):", body, re.MULTILINE))
    assert fields == ProviderRetry.__optional_keys__


async def test_retained_workers_answer_with_their_worker_id(tmp_path: Path) -> None:
    turns = [subagent_call(), text_turn("child answer"), text_turn("done")]
    cfg = config(
        turns,
        mode="skip_dangerous",
        subagents=True,
        config_dir=str(tmp_path),
        workers={"retain": True},
    )
    envelopes = await run_once(cfg)

    assert tools_of(envelopes) == ["Subagent", "SubagentContinue"]
    [answer] = results(envelopes)
    assert answer.startswith("child answer\n\n[Worker ID: "), answer


async def test_background_workers_can_also_be_stopped(tmp_path: Path) -> None:
    cfg = config(
        [text_turn("hi")],
        subagents=True,
        config_dir=str(tmp_path),
        workers={"background": True},
    )
    assert tools_of(await run_once(cfg)) == ["Subagent", "SubagentContinue", "TaskStop"]


async def test_workers_need_subagents() -> None:
    async with Server() as server:
        with pytest.raises(OpenError) as raised:
            await server.open_agent(config([text_turn("hi")], workers={"retain": True}))
    assert "workers need subagents" in str(raised.value)


async def test_a_mailbox_delivers_a_message_to_another_session() -> None:
    turns = [
        tool_turn("c1", "send_message", {"to": "b", "content": "hello b"}),
        text_turn("sent"),
        text_turn("read it"),
    ]
    async with Server() as server:
        agent = await server.open_agent(config(turns, mode="skip_dangerous", mailbox=True))
        first = [e async for e in await agent.run("a", "tell b")]
        second = [e async for e in await agent.run("b", "anything new?")]

    assert "send_message" in tools_of(first)
    assert results(first) == ["Message delivered to 'b'."]
    prompt = next(e.event for e in second if isinstance(e.event, UserEvent))
    texts = [block.get("text", "") for block in prompt.message["content"]]
    assert "<peer-message><from>a</from><type>message</type>" in texts[-1]
    assert "hello b" in texts[-1]


async def test_tool_search_adds_its_tool() -> None:
    on = tools_of(
        await run_once(config([text_turn("hi")], tasks=True, tool_search={"max_tools": 2}))
    )
    assert on == [*TASKS, "tool_search"]


async def test_skills_load_from_the_config_dir(tmp_path: Path) -> None:
    skill = tmp_path / "skills" / "notes" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\ndescription: Take notes.\n---\nNotes for $ARGUMENTS.", encoding="utf-8")
    turns = [tool_turn("c1", "Skill", {"skill": "notes", "args": "prod"}), text_turn("Done.")]
    envelopes = await run_once(config(turns, skills=True, config_dir=str(tmp_path)))

    assert tools_of(envelopes) == ["Skill"]
    [text] = results(envelopes)
    assert text.endswith("\n\nNotes for prod."), text


async def test_bash_off_leaves_it_out_of_the_workspace_tools(tmp_path: Path) -> None:
    on = tools_of(await run_once(config([text_turn("hi")], workspace=str(tmp_path))))
    off = tools_of(await run_once(config([text_turn("hi")], workspace=str(tmp_path), bash=False)))

    assert "Bash" in on and "Read" in on
    assert off == [name for name in on if name != "Bash"]


async def test_the_filesystem_tools_keep_files_for_the_session() -> None:
    turns = [
        tool_turn("c1", "write_file", {"path": "/notes.md", "content": "remember this"}),
        tool_turn("c2", "read_file", {"path": "/notes.md"}),
        text_turn("done"),
    ]
    envelopes = await run_once(config(turns, mode="skip_dangerous", filesystem={}))

    assert tools_of(envelopes) == ["ls", "read_file", "write_file", "edit_file"]
    assert results(envelopes)[1] == "remember this"


DEEP_AGENT_TOOLS = sorted(
    [*TASKS, "ls", "read_file", "write_file", "edit_file"]
    + ["Skill", "Subagent", "SubagentContinue", "TaskStop"]
)


async def test_a_deep_agent_plans_with_tasks_and_can_delegate() -> None:
    turns = [
        tool_turn("c1", "TaskCreate", {"subject": "Plan", "description": "Steps"}),
        text_turn("planned"),
    ]
    envelopes = await run_once(config(turns, deep_agent={"durable": False}))

    assert sorted(tools_of(envelopes)) == DEEP_AGENT_TOOLS
    assert results(envelopes) == ["Task #1 created: Plan"]
    assert final(envelopes).final_text == "planned", "no approval was asked"


async def test_a_durable_deep_agent_needs_postgres_stores() -> None:
    async with Server() as server:
        with pytest.raises(OpenError) as raised:
            await server.open_agent(config([], deep_agent={"profile": "balanced"}))
    assert "Postgres stores" in str(raised.value)


async def test_a_profile_denies_its_tools() -> None:
    seen: list[str] = []

    @tool(scope="read")
    async def lookup(email: str) -> str:
        """Find a customer."""
        seen.append(email)
        return "Ada"

    turns = [tool_turn("c1", "lookup", {"email": "ada@example.com"}), text_turn("Cannot.")]
    support: Profile = {"id": "support", "version": "2", "tool_deny": ["lookup"], "max_turns": 5}
    async with Server() as server:
        agent = await server.open_agent(config(turns, profile=support), tools=[lookup])
        envelopes = [e async for e in await agent.run("s1", "Who is ada@example.com?")]

    [end] = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
    assert end.is_error and seen == [], "a denied tool never runs"
    assert final(envelopes).final_text == "Cannot."


async def test_an_invalid_profile_does_not_open() -> None:
    async with Server() as server:
        with pytest.raises(OpenError) as raised:
            await server.open_agent(config([], profile={"id": "", "version": "1"}))
    assert "profile id must not be blank" in str(raised.value)
