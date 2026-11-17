"""Live: durability of runs against a real model. A server is killed
(SIGKILL) in the middle of a tool, of a provider call and while a run is
suspended on an approval; another server on the same database resumes the
run. A server that is frozen (SIGSTOP) past its lease loses the run to
another server and is fenced out when it wakes up. Skips without the live
environment.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
from collections import Counter
from typing import Any

import pytest
from live_sched_support import (
    kind,
    live_config,
    once_free,
    pg_dsn,
    seqs_increasing,
    vllm,
)

from linch_rust import (
    Allow,
    ConnectionClosed,
    Envelope,
    ResultEvent,
    Server,
    SuspendedEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UsageEvent,
    tool,
)

__all__ = ["kind", "pg_dsn", "vllm"]

pytestmark = pytest.mark.live


def lookup_tool(calls: list[str], *, block: asyncio.Event | None, started: asyncio.Event) -> Any:
    """The `slow_lookup` host tool. The same name, description and schema
    every time, so every server computes the same run contract; `block`
    makes a call hang until it is set (never, when it is None and the call
    is expected to hang)."""

    @tool(scope="read")
    async def slow_lookup(topic: str) -> str:
        """Look up a topic in the archive (slow)."""
        calls.append(topic)
        started.set()
        if block is not None:
            await block.wait()
            return f"{topic}: found"
        return f"{topic}: found"

    return slow_lookup


def save_tool(calls: list[str]) -> Any:
    @tool(scope="write")
    async def save_note(text: str) -> str:
        """Save a note."""
        calls.append(text)
        return f"saved: {text}"

    return save_note


def usage_totals(events: list[Envelope]) -> tuple[Counter[str], dict[str, Any]]:
    """The sum of the durable usage events, and the result's `total_usage`."""
    total: Counter[str] = Counter()
    for envelope in events:
        if isinstance(envelope.event, UsageEvent):
            for key, value in envelope.event.usage.items():
                if isinstance(value, int):
                    total[key] += value
    result = events[-1].event
    assert isinstance(result, ResultEvent)
    return total, dict(result.total_usage)


def assert_one_clean_history(events: list[Envelope]) -> None:
    assert seqs_increasing(events)
    results = [e for e in events if isinstance(e.event, ResultEvent)]
    assert len(results) == 1 and events[-1] is results[0], "exactly one result, and last"
    total, reported = usage_totals(events)
    for key, value in total.items():
        if key in reported and isinstance(reported[key], int):
            assert reported[key] == value, f"{key}: usage events {value}, result {reported[key]}"


ORIGINAL = "Call the tool slow_lookup with topic 'otters', then say what it returned."


async def test_a_kill_during_a_tool_resumes_without_running_it_twice(
    kind: str, pg_dsn: str
) -> None:
    config = live_config(kind, "kill-tool", pg_dsn)
    first_calls: list[str] = []
    started = asyncio.Event()
    async with Server() as server:
        agent = await server.open_agent(
            config, tools=[lookup_tool(first_calls, block=asyncio.Event(), started=started)]
        )
        run = await agent.run("s1", ORIGINAL)
        await asyncio.wait_for(started.wait(), 90)
        os.kill(server.pid, signal.SIGKILL)
        with pytest.raises(ConnectionClosed):
            await run.result()
    assert len(first_calls) == 1

    second_calls: list[str] = []
    async with Server() as server:
        agent = await server.open_agent(
            config, tools=[lookup_tool(second_calls, block=None, started=asyncio.Event())]
        )
        result = await once_free(lambda: agent.resume(run.id))
        events = await agent.events(run.id)

    assert isinstance(result, ResultEvent) and result.subtype == "success", result
    assert_one_clean_history(events)
    starts = [e.event for e in events if isinstance(e.event, ToolCallStartEvent)]
    [original] = [s for s in starts if s.tool_name == "slow_lookup"][:1]
    ends = [e.event for e in events if isinstance(e.event, ToolCallEndEvent)]
    [interrupted] = [end for end in ends if end.tool_use_id == original.tool_use_id]
    assert interrupted.is_error and "interrupted" in str(interrupted.result), interrupted
    assert Counter(s.tool_use_id for s in starts)[original.tool_use_id] == 1, "started once"
    # The resumed model may choose to look the topic up again, as a new call.
    assert len(second_calls) <= 1


async def test_a_kill_during_a_provider_call_resumes_and_pays_once(vllm: str, pg_dsn: str) -> None:
    config = live_config(vllm, "kill-call", pg_dsn, spec={"max_output_tokens": 1200})
    prompt = "Write the integers from 1 to 150, one per line, and nothing else."
    async with Server() as server:
        agent = await server.open_agent(config)
        run = await agent.run("s1", prompt)
        streamed = 0
        with pytest.raises(ConnectionClosed):
            async for envelope in run:
                if envelope.event.type == "partial_assistant":
                    streamed += 1
                    if streamed == 3:
                        os.kill(server.pid, signal.SIGKILL)
    assert streamed == 3

    async with Server() as server:
        agent = await server.open_agent(config)
        result = await once_free(lambda: agent.resume(run.id))
        events = await agent.events(run.id)

    assert isinstance(result, ResultEvent) and result.subtype == "success", result
    assert_one_clean_history(events)
    usage = [e for e in events if isinstance(e.event, UsageEvent)]
    assistants = [e for e in events if e.event.type == "assistant"]
    assert len(usage) == len(assistants) == 1, "the killed call left no receipt, the retry one"
    assert "150" in (result.final_text or "")


async def test_a_kill_while_suspended_on_an_approval_resumes_with_the_approval(
    kind: str, pg_dsn: str
) -> None:
    config = live_config(
        kind, "kill-approve", pg_dsn, permissions={"mode": "default", "project_root": "/"}
    )
    prompt = "Call the tool save_note with text 'call back at noon', then say it was saved."
    first_calls: list[str] = []
    async with Server() as server:
        agent = await server.open_agent(config, tools=[save_tool(first_calls)])
        run = await agent.run("s1", prompt)
        suspended = await run.result()
        assert isinstance(suspended, SuspendedEvent), suspended
        os.kill(server.pid, signal.SIGKILL)
    assert first_calls == [], "nothing runs before the approval"

    second_calls: list[str] = []
    async with Server() as server:
        agent = await server.open_agent(config, tools=[save_tool(second_calls)])
        # The approval of a run another process suspended, after a kill.
        approved = await agent.approve(
            run.id, {rid: Allow() for rid in suspended.pending_request_ids}
        )
        result = await approved.result()
        events = await agent.events(run.id)

    assert isinstance(result, ResultEvent) and result.subtype == "success", result
    assert second_calls == ["call back at noon"], "approved, and run exactly once"
    assert_one_clean_history(events)
    ends = [e.event for e in events if isinstance(e.event, ToolCallEndEvent)]
    assert [end.tool_name for end in ends] == ["save_note"]


async def test_a_frozen_server_loses_its_run_and_is_fenced_out(vllm: str, pg_dsn: str) -> None:
    config = live_config(vllm, "fence", pg_dsn)  # a 2 s lease
    release = asyncio.Event()
    started = asyncio.Event()
    old_calls: list[str] = []
    async with Server() as old, Server() as new:
        old_agent = await old.open_agent(
            config, tools=[lookup_tool(old_calls, block=release, started=started)]
        )
        run = await old_agent.run("s1", ORIGINAL)
        await asyncio.wait_for(started.wait(), 90)

        # While the old server holds the lease, the new one is refused.
        new_calls: list[str] = []
        new_agent = await new.open_agent(
            config, tools=[lookup_tool(new_calls, block=None, started=asyncio.Event())]
        )
        taken = await new_agent.resume(run.id)
        from linch_rust import RunLeased

        with pytest.raises(RunLeased) as refused:
            await taken.result()
        # The refusal names the holder by an opaque server id (8 hex characters), not by a
        # constant name shared by every server.
        owner = refused.value.owner
        assert owner != "linch-rpc" and re.fullmatch(r"[0-9a-f]{8}", owner), owner

        # Frozen past its lease, the old server no longer holds the run.
        os.kill(old.pid, signal.SIGSTOP)
        try:
            await asyncio.sleep(4)
            result = await once_free(lambda: new_agent.resume(run.id))
            settled = await new_agent.events(run.id)
        finally:
            os.kill(old.pid, signal.SIGCONT)
        assert isinstance(result, ResultEvent) and result.subtype == "success", result
        assert_one_clean_history(settled)

        # Woken up, the old server's tool returns and its run tries to go on.
        release.set()
        try:
            await asyncio.wait_for(run.result(), 30)
        except Exception:  # fenced out: any ending but a write is acceptable
            pass
        after = await new_agent.events(run.id)

    assert [(e.seq, e.event) for e in after] == [(e.seq, e.event) for e in settled], (
        "the fenced server wrote nothing to the run"
    )
    assert_one_clean_history(after)
