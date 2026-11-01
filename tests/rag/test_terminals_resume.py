from __future__ import annotations

from typing import Any

import pytest
from test_rag import Connector, Provider, drain, make_agent


@pytest.mark.parametrize(
    "payload,success",
    [
        ({"nested": [{"answer": "[doc:a]"}]}, True),
        ({"[doc:a]": "unsupported"}, False),
        ({"nested": ["[doc:a]", "[doc:unknown]"]}, False),
    ],
)
async def test_final_tool_checks_nested_values_before_ack(payload, success):
    import json

    from linch import ToolRegistry, Usage, register_grounding_check, register_rag_connector, tool

    @tool
    async def finish(nested: list[Any] | None = None) -> str:
        raise AssertionError("terminal tool must never execute")

    class Terminal(Provider):
        async def stream(self, req):
            yield {"type": "message_start", "model": req.model}
            yield {"type": "tool_use_start", "id": "final", "name": "finish"}
            yield {"type": "tool_use_input_delta", "id": "final", "json_delta": json.dumps(payload)}
            yield {"type": "tool_use_end", "id": "final"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}

    tools = ToolRegistry()
    tools.register(finish)
    agent = make_agent(Terminal(), reliability=None, final_tool_name="finish")
    agent.tools = tools
    register_rag_connector(agent, Connector())
    register_grounding_check(agent)
    events = await drain(await agent.session())
    assert (events[-1].subtype == "success") is success
    assert not any(event.type == "tool_call_end" for event in events)
    await agent.close()


@pytest.mark.parametrize("answer,success", [("unsupported", False), ("[doc:a]", True)])
async def test_forced_final_runs_grounding(answer, success):
    from linch import ToolRegistry, Usage, register_grounding_check, register_rag_connector, tool

    @tool
    async def repeat() -> str:
        return "again"

    class Repeating(Provider):
        async def stream(self, req):
            self.requests.append(req)
            yield {"type": "message_start", "model": req.model}
            if req.tools:
                yield {"type": "tool_use_start", "id": str(len(self.requests)), "name": "repeat"}
                yield {
                    "type": "tool_use_input_delta",
                    "id": str(len(self.requests)),
                    "json_delta": "{}",
                }
                yield {"type": "tool_use_end", "id": str(len(self.requests))}
            else:
                yield {"type": "text_delta", "text": answer}
            yield {
                "type": "message_end",
                "stop_reason": "tool_use" if req.tools else "end_turn",
                "usage": Usage(),
            }

    tools = ToolRegistry()
    tools.register(repeat)
    provider = Repeating()
    agent = make_agent(
        provider,
        reliability=None,
        loop_guard={"max_identical_tool_calls": 1, "force_final_answer": True},
    )
    agent.tools = tools
    register_rag_connector(agent, Connector())
    register_grounding_check(agent)
    events = await drain(await agent.session())
    assert any(not request.tools for request in provider.requests)
    assert (events[-1].subtype == "success") is success
    await agent.close()


class ProcessDeath(BaseException):
    pass


class CrashStore:
    def __init__(self, inner, phase):
        self.inner, self.phase, self.dead = inner, phase, False

    def __getattr__(self, name):
        value = getattr(self.inner, name)
        if not callable(value):
            return value

        async def call(*args, **kwargs):
            if self.dead:
                raise ProcessDeath()
            result = await value(*args, **kwargs)
            if name.startswith("save_checkpoint") and args[1].phase == self.phase:
                self.dead = True
                raise ProcessDeath()
            return result

        return call


@pytest.mark.parametrize("phase", ["assistant_appended", "turn_complete", "provider_pending"])
@pytest.mark.parametrize("answer,success", [("unsupported", False), ("[doc:a]", True)])
async def test_durable_resume_preserves_query_citations_and_retry_count(phase, answer, success):
    from linch import (
        DurabilityOptions,
        InMemoryRunStore,
        ReliabilityPolicy,
        register_grounding_check,
        register_rag_connector,
    )

    inner = InMemoryRunStore()
    crash = CrashStore(inner, phase)
    provider = Provider(["unsupported", answer])
    connector = Connector()
    agent = make_agent(
        provider,
        run_store=crash,
        durability=DurabilityOptions(exact_model_input=True),
        reliability=ReliabilityPolicy(max_correction_turns=1),
    )
    register_rag_connector(agent, connector)
    register_grounding_check(agent)
    session = await agent.session()
    run_id = ""
    with pytest.raises(ProcessDeath):
        async for event in session.run("original question"):
            if event.type == "system":
                run_id = event.run_id
    stored = await inner.load_run(run_id)
    assert "linch.rag" in stored.checkpoint.extension_state
    resumed_connector = Connector()
    resumed = make_agent(
        provider,
        run_store=inner,
        durability=DurabilityOptions(exact_model_input=True),
        reliability=ReliabilityPolicy(max_correction_turns=1),
    )
    resumed.session_store = session.store
    register_rag_connector(resumed, resumed_connector)
    register_grounding_check(resumed)
    reopened = await resumed.session(id=session.id)
    events = [event async for event in reopened.resume(run_id)]
    assert (events[-1].subtype == "success") is success
    assert len(provider.requests) == 2
    assert all(query == "original question" for query, _, _ in resumed_connector.calls)
    assert "PRIVATE BODY" not in str(reopened.full_history)
    rows = await inner.load_events(run_id)
    assert (
        sum(row.event.type == "verification" and row.event.action == "retry" for row in rows) == 1
    )
    await resumed.close()


async def test_alignment_replaces_retrieval_query_on_next_request():
    import asyncio

    from linch import ReliabilityPolicy, register_grounding_check, register_rag_connector

    entered, release = asyncio.Event(), asyncio.Event()

    class Pausing(Provider):
        async def stream(self, request):
            if not self.requests:
                entered.set()
                await release.wait()
            async for event in super().stream(request):
                yield event

    provider, connector = Pausing(["unsupported", "[doc:a]"]), Connector()
    agent = make_agent(provider, reliability=ReliabilityPolicy(max_correction_turns=1))
    register_rag_connector(agent, connector)
    register_grounding_check(agent)
    session = await agent.session()
    task = asyncio.create_task(drain(session))
    await entered.wait()
    alignment = asyncio.create_task(session.align("new question"))
    await asyncio.sleep(0)
    release.set()
    events = await task
    await alignment
    assert events[-1].subtype == "success"
    assert [query for query, _, _ in connector.calls] == ["original question", "new question"]
    await agent.close()


async def test_citations_in_every_text_block_are_checked():
    from linch import HookResult, TextBlock, register_grounding_check, register_rag_connector

    class Extra:
        def on_after_provider_call(self, ctx):
            ctx.assembly.message.content.append(TextBlock(text="extra [doc:unknown]"))
            return HookResult.mutate(assembly=ctx.assembly)

    agent = make_agent(hooks=[Extra()], reliability=None)
    register_rag_connector(agent, Connector())
    register_grounding_check(agent)
    events = await drain(await agent.session())
    assert events[-1].subtype == "error"
    await agent.close()
