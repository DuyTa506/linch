"""Correction feedback and its checkpoint survive process loss as one logical turn."""

from __future__ import annotations

from typing import Any

import pytest
from test_terminal_atomicity import _CrashWrites, _normalized, _ProcessDeath

from tests.model_calls.test_model_call_journal import _close, _reopen, _stores
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn


class CorrectionWrites(_CrashWrites):
    def __init__(self, *args, nth=1):
        super().__init__(*args)
        self.nth, self.hits = nth, 0

    async def _write(self, name: str, *args: Any, **kw: Any) -> Any:
        hit = (
            name == "append_messages"
            and any("[Linch]" in str(message) for message in args[1])
            and self.point == "feedback"
        ) or (
            name.startswith("save_checkpoint")
            and args[1].phase
            == ("assistant_appended" if self.point == "assistant" else "turn_complete")
            and self.point in {"checkpoint", "assistant"}
        )
        if hit:
            self.hits += 1
            hit = self.hits == self.nth
        original = self.point
        self.point = name if hit else ""
        self.armed = True
        # Avoid the parent's guard-specific turn_complete counter.
        if name.startswith("save_checkpoint") and args[1].phase == "turn_complete":
            self.point = f"turn_complete:{self.turn_complete + 1}" if hit else ""
        try:
            return await super()._write(name, *args, **kw)
        finally:
            self.point = original


class ToolEndCrash(_CrashWrites):
    target_tool = "first"
    at_pending = False

    async def _write(self, name, *args, **kwargs):
        if (
            not self.at_pending
            and name == "append_events"
            and any(
                event.type == "tool_call_end" and event.tool_name == self.target_tool
                for event in args[1]
            )
        ) or (
            self.at_pending
            and name.startswith("save_checkpoint")
            and args[1].phase == "tool_batch_pending"
            and args[1].completed_tool_results
        ):
            self.point, self.armed, self.after = name, True, True
        return await super()._write(name, *args, **kwargs)


class CorrectionProvider:
    id = "correction-crash"

    def __init__(self, case: str) -> None:
        self.case, self.calls = case, 0

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, request: Any) -> Any:
        from linch.types import Usage

        self.calls += 1
        yield {"type": "message_start", "model": request.model}
        invalid = self.calls <= (2 if self.case.endswith("_twice") else 1)
        if self.case.startswith("native") and invalid:
            yield {"type": "tool_use_start", "id": "bad", "name": "prepare"}
            yield {"type": "tool_use_input_delta", "id": "bad", "json_delta": '{"secret":"raw"'}
            yield {"type": "tool_use_end", "id": "bad"}
            stop = "tool_use"
        elif self.case in {"completion", "final_tool"} and self.calls == 2:
            yield {"type": "tool_use_start", "id": "good", "name": "prepare"}
            yield {"type": "tool_use_input_delta", "id": "good", "json_delta": "{}"}
            yield {"type": "tool_use_end", "id": "good"}
            stop = "tool_use"
        elif self.case == "final_tool":
            yield {"type": "tool_use_start", "id": "final", "name": "finish"}
            yield {"type": "tool_use_input_delta", "id": "final", "json_delta": "{}"}
            yield {"type": "tool_use_end", "id": "final"}
            stop = "tool_use"
        else:
            text = (
                '<tool_call>{"secret":"raw"}</tool_call>'
                if self.case.startswith("text") and invalid
                else "answer"
            )
            yield {"type": "text_delta", "text": text}
            stop = "end_turn"
        yield {"type": "message_end", "stop_reason": stop, "usage": Usage(3, 2)}


def correction_agent(sessions: Any, runs: Any, provider: Any) -> Any:
    from linch import Agent, CompletionGate, ReliabilityPolicy
    from linch.config import FeatureFlags
    from linch.tools import tool
    from linch.tools.registry import empty_tools

    @tool
    async def prepare() -> str:
        return "prepared"

    @tool
    async def finish() -> str:
        raise AssertionError("final tool must never execute")

    return Agent(
        model="test",
        provider=provider,
        session_store=sessions,
        run_store=runs,
        tools=empty_tools(prepare, finish),
        final_tool_name="finish" if provider.case == "final_tool" else None,
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        reliability=ReliabilityPolicy(
            max_correction_turns=2 if provider.case.endswith("_twice") else 1,
            completion=CompletionGate(("prepare",))
            if provider.case in {"completion", "final_tool"}
            else None,
        ),
    )


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("after", [False, True], ids=["before", "after"])
@pytest.mark.parametrize("point", ["feedback", "checkpoint"])
@pytest.mark.parametrize(
    "case", ["text", "native", "completion", "final_tool", "text_twice", "native_twice"]
)
async def test_correction_crash_exact_trace(pg_dsn, kind, after, point, case):
    from linch.events import PromptCacheAdvisoryEvent, event_to_dict

    corrections = 2 if case.endswith("_twice") else 1

    async def scenario(crash: bool):
        sessions, runs = await _stores(kind, pg_dsn)
        sw = CorrectionWrites(sessions, point if crash else "", after, nth=corrections)
        rw = CorrectionWrites(runs, point if crash else "", after, nth=corrections)
        sw.peer, rw.peer = rw, sw
        provider = CorrectionProvider(case)
        session = await correction_agent(sw, rw, provider).session()
        run_id = ""
        try:
            try:
                async for event in session.run("go"):
                    if event.type == "system":
                        run_id = event.run_id
            except _ProcessDeath:
                assert crash
            else:
                assert not crash, "correction crash boundary was not reached"
            if crash:
                sessions, runs = await _reopen(kind, pg_dsn, sessions, runs)
                session = await correction_agent(sessions, runs, provider).session(id=session.id)
                events = [event async for event in session.resume(run_id)]
                assert events[-1].type == "result" and events[-1].subtype == "success"
            rows = await runs.load_events(run_id)
            record = await runs.load_run(run_id)
            assert record.status == "completed", [
                event_to_dict(row.event)
                for row in rows
                if row.event.type in {"error", "reliability"}
            ]
            assert provider.calls == (
                3 if case in {"completion", "final_tool"} else corrections + 1
            )
            assert (
                record.checkpoint.extension_state["linch.reliability"]["corrections"] == corrections
            )
            assert sum(row.event.type == "reliability" for row in rows) == corrections
            assert (
                sum(
                    row.event.type == "user" and row.event.subtype == "notification" for row in rows
                )
                == corrections
            )
            assert sum(
                row.event.type == "assistant"
                and "The provider response was rejected" in str(row.event.message)
                for row in rows
            ) == (corrections if case.startswith("text") else 0)
            assert (
                sum(
                    row.event.type == "model_call" and row.event.record_kind == "receipt"
                    for row in rows
                )
                == provider.calls
            )
            if case.startswith(("text", "native")):
                assert (
                    sum(
                        message.role == "user"
                        and "The provider response was invalid" in str(message)
                        for message in session.full_history
                    )
                    == corrections
                )
                assert sum(
                    message.role == "assistant"
                    and "The provider response was rejected" in str(message)
                    for message in session.full_history
                ) == (corrections if case.startswith("text") else 0)
            assert "raw" not in str(session.full_history)
            return [
                _normalized(event_to_dict(row.event))
                for row in rows
                if not isinstance(row.event, PromptCacheAdvisoryEvent)
            ]
        finally:
            await _close(sessions, runs)

    assert await scenario(True) == await scenario(False)


@pytest.mark.parametrize("raw", [None, {"v": 2, "corrections": 0, "succeeded": []}])
async def test_resume_poisoned_reliability_namespace_fails_closed(raw):
    from linch.errors import ConfigError

    sessions, runs = await _stores("memory", "")
    provider = CorrectionProvider("text")
    sw = CorrectionWrites(sessions, "feedback")
    rw = CorrectionWrites(runs)
    sw.peer, rw.peer = rw, sw
    session = await correction_agent(sw, rw, provider).session()
    run_id = ""
    with pytest.raises(_ProcessDeath):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    record = await runs.load_run(run_id)
    record.checkpoint.extension_state["linch.reliability"] = raw
    await runs.save_checkpoint(run_id, record.checkpoint)
    resumed = await correction_agent(sessions, runs, provider).session(id=session.id)
    with pytest.raises((ConfigError, ValueError), match="checkpoint state"):
        _ = [event async for event in resumed.resume(run_id)]
    assert provider.calls == 1


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("crash", [False, True])
@pytest.mark.parametrize("parallel", [False, True])
async def test_resumed_batch_cannot_unlock_its_own_prerequisite(pg_dsn, kind, crash, parallel):
    from linch import Agent, ReliabilityPolicy, ToolPrerequisite
    from linch.tools import tool
    from linch.tools.registry import empty_tools
    from linch.types import Usage

    calls = []

    @tool(scope="read" if parallel else "write", parallel=parallel)
    async def first() -> str:
        calls.append("first")
        return "success"

    @tool(scope="read" if parallel else "write", parallel=parallel)
    async def second() -> str:
        calls.append("second")
        return "success"

    class Provider(CorrectionProvider):
        async def stream(self, request):
            self.calls += 1
            yield {"type": "message_start", "model": request.model}
            if self.calls == 1:
                for name in ("first", "second"):
                    yield {"type": "tool_use_start", "id": name, "name": name}
                    yield {"type": "tool_use_input_delta", "id": name, "json_delta": "{}"}
                    yield {"type": "tool_use_end", "id": name}
            else:
                yield {"type": "text_delta", "text": "done"}
            yield {
                "type": "message_end",
                "stop_reason": "tool_use" if self.calls == 1 else "end_turn",
                "usage": Usage(),
            }

    sessions, runs = await _stores(kind, pg_dsn)
    provider = Provider("batch")

    def agent(store):
        return Agent(
            model="test",
            provider=provider,
            session_store=sessions,
            run_store=store,
            tools=empty_tools(first, second),
            permissions={"mode": "skip-dangerous"},
            reliability=ReliabilityPolicy(prerequisites=(ToolPrerequisite("second", ("first",)),)),
        )

    session = await agent(ToolEndCrash(runs) if crash else runs).session()
    run_id = ""
    try:
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    except _ProcessDeath:
        assert crash
        assert "linch.reliability" not in (await runs.load_run(run_id)).checkpoint.extension_state
        sessions, runs = await _reopen(kind, pg_dsn, sessions, runs)
        session = await agent(runs).session(id=session.id)
        _ = [event async for event in session.resume(run_id)]
    else:
        assert not crash
    assert calls == ["first"]
    rows = await runs.load_events(run_id)
    assert [row.event.tool_name for row in rows if row.event.type == "tool_call_end"] == (
        ["first"] if crash and parallel else ["first", "second"]
    )
    results = [
        block.tool_use_id
        for message in session.full_history
        for block in message.content
        if block.type == "tool_result"
    ]
    assert results == ["first", "second"]
    assert next(
        block
        for message in session.full_history
        for block in message.content
        if block.type == "tool_result" and block.tool_use_id == "second"
    ).is_error
    assert (await runs.load_run(run_id)).checkpoint.extension_state["linch.reliability"][
        "succeeded"
    ] == ["first"]

    await _close(sessions, runs)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("hooked", [False, True])
async def test_literal_diagnostic_resumes_as_ordinary_answer(enabled, after, hooked):
    from linch import Agent, HookResult, ReliabilityPolicy
    from linch.loop.turn_stopping import INVALID_RESPONSE_DIAGNOSTIC

    class Host:
        resume_policy_id = "test.literal-diagnostic"
        resume_policy_config = {"hooked": hooked}

        def on_after_provider_call(self, ctx):
            return (
                HookResult.force_continue("host feedback")
                if hooked and ctx.turn_index == 0
                else None
            )

    sessions, runs = await _stores("memory", "")
    provider = ScriptedProvider([TextTurn(INVALID_RESPONSE_DIAGNOSTIC), TextTurn("done")])

    def agent(store):
        return Agent(
            model="test",
            provider=provider,
            session_store=sessions,
            run_store=store,
            hooks=[Host()],
            reliability=ReliabilityPolicy() if enabled else None,
        )

    session = await agent(CorrectionWrites(runs, "assistant", after)).session()
    run_id = ""
    with pytest.raises(_ProcessDeath):
        async for event in session.run("go"):
            if event.type == "system":
                run_id = event.run_id
    resumed = await agent(runs).session(id=session.id)
    events = [event async for event in resumed.resume(run_id)]
    assert events[-1].subtype == "success"
    assert events[-1].final_text == ("done" if hooked else INVALID_RESPONSE_DIAGNOSTIC)
    assert provider._index == (2 if hooked else 1)
    assert not any(row.event.type == "reliability" for row in await runs.load_events(run_id))


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("crash", [0, 1, 2])
async def test_hook_created_background_ack_never_unlocks_dependency(pg_dsn, kind, crash):
    from linch import Agent, HookResult, ReliabilityPolicy, ToolPrerequisite
    from linch.tools import tool
    from linch.tools.registry import empty_tools

    calls = []

    @tool(name="Subagent")
    async def launch(run_in_background: bool = False) -> str:
        assert run_in_background
        calls.append("launch")
        return "Background worker launched"

    @tool
    async def dependent() -> str:
        calls.append("dependent")
        return "must not execute"

    class Mutate:
        resume_policy_id = "test.background-input"
        resume_policy_config = {}

        def on_pre_tool_use(self, ctx):
            return (
                HookResult.mutate(input={"run_in_background": True})
                if ctx.tool_name == "Subagent"
                else None
            )

    sessions, runs = await _stores(kind, pg_dsn)
    provider = ScriptedProvider(
        [
            ToolUseTurn("Subagent", {}, "launch"),
            ToolUseTurn("dependent", {}, "dependent"),
            TextTurn("done"),
        ]
    )

    def agent(store):
        return Agent(
            model="test",
            provider=provider,
            session_store=sessions,
            run_store=store,
            tools=empty_tools(launch, dependent),
            hooks=[Mutate()],
            enable_background_tools=False,
            permissions={"mode": "skip-dangerous"},
            reliability=ReliabilityPolicy(
                prerequisites=(ToolPrerequisite("dependent", ("Subagent",)),)
            ),
        )

    fault = ToolEndCrash(runs)
    fault.target_tool = "Subagent"
    session = await agent(fault if crash else runs).session()
    run_id = ""
    try:
        try:
            async for event in session.run("go"):
                if event.type == "system":
                    run_id = event.run_id
        except _ProcessDeath:
            assert crash
            batch_cursor = (await runs.load_run(run_id)).checkpoint.tool_batch_event_after_seq
            sessions, runs = await _reopen(kind, pg_dsn, sessions, runs)
            repeat = ToolEndCrash(runs)
            repeat.at_pending = True
            session = await agent(repeat if crash == 2 else runs).session(id=session.id)
            try:
                _ = [event async for event in session.resume(run_id)]
            except _ProcessDeath:
                assert crash == 2
                pending = (await runs.load_run(run_id)).checkpoint
                assert pending.completed_tool_results["launch"].is_error is False
                assert pending.tool_batch_event_after_seq == batch_cursor
                sessions, runs = await _reopen(kind, pg_dsn, sessions, runs)
                session = await agent(runs).session(id=session.id)
                _ = [event async for event in session.resume(run_id)]
            else:
                assert crash != 2
        else:
            assert not crash
        assert calls == ["launch"]
        record = await runs.load_run(run_id)
        assert "linch.reliability" not in record.checkpoint.extension_state
        rows = await runs.load_events(run_id)
        assert next(row.event for row in rows if row.event.type == "tool_call_start").input == {
            "run_in_background": True
        }
        assert (
            next(
                block
                for message in session.full_history
                for block in message.content
                if block.type == "tool_use" and block.id == "launch"
            ).input
            == {}
        )
        assert next(
            row.event
            for row in rows
            if row.event.type == "tool_call_end" and row.event.tool_name == "dependent"
        ).is_error
    finally:
        await _close(sessions, runs)
