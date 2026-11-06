"""A run end to end, event replay, errors, and the server's lifetime."""

from __future__ import annotations

import os
from typing import cast

import pytest
from scripted import config, text_turn

from linch_rust import (
    AgentConfig,
    AssistantEvent,
    InitEvent,
    OpenError,
    ProtocolError,
    ResultEvent,
    RunNotFound,
    Server,
    UsageEvent,
)

STREAM_ONLY = {"partial_assistant", "tool_progress", "hook"}


async def test_a_run_streams_its_envelopes_then_its_result() -> None:
    async with Server() as server:
        agent = await server.open_agent(config([text_turn("Hello there.")]))
        run = await agent.run("s1", "Say hello")
        envelopes = [envelope async for envelope in run]
        result = await run.result()

    kinds = [envelope.event.type for envelope in envelopes]
    assert kinds[:2] == ["init", "user"]
    assert kinds[-1] == "result"
    assert all(envelope.run_id == run.id for envelope in envelopes)
    init = envelopes[0].event
    assert isinstance(init, InitEvent) and init.session_id == "s1"
    assistant = next(e.event for e in envelopes if isinstance(e.event, AssistantEvent))
    assert assistant.stop_reason == "end_turn"
    assert isinstance(result, ResultEvent)
    assert result.subtype == "success"
    assert result.final_text == "Hello there."
    assert all(envelope.ts_ms > 1_700_000_000_000 for envelope in envelopes)
    assert result.duration_ms >= 0
    assert result.total_cost_usd is None, "the scripted model has no price"


async def test_a_priced_model_reports_what_the_run_cost() -> None:
    async with Server() as server:
        agent = await server.open_agent(config([text_turn("Hi.", model="claude-opus-4-8")]))
        run = await agent.run("s1", "Say hi")
        envelopes = [envelope async for envelope in run]
        result = await run.result()

    [usage] = [e.event for e in envelopes if isinstance(e.event, UsageEvent)]
    assert usage.cost_usd is not None and usage.cost_usd > 0
    assert isinstance(result, ResultEvent)
    assert result.total_cost_usd == usage.cumulative_cost_usd == usage.cost_usd


async def test_events_replays_the_durable_envelopes_after_a_seq() -> None:
    async with Server() as server:
        agent = await server.open_agent(config([text_turn("Replayed.")]))
        run = await agent.run("s1", "Go")
        streamed = [e async for e in run if e.event.type not in STREAM_ONLY]
        replayed = await agent.events(run.id)
        middle = streamed[1].seq
        tail = await agent.events(run.id, after_seq=middle)

    assert replayed == streamed
    assert [e.seq for e in replayed] == sorted({e.seq for e in replayed})
    assert tail == [e for e in streamed if e.seq > middle]


async def test_an_invalid_config_is_refused_at_open() -> None:
    async with Server() as server:
        # Only a cast gets an unknown field past the type checker.
        bad = cast(AgentConfig, {**config([text_turn("x")]), "api_key": "sk-never"})
        with pytest.raises(ProtocolError) as raised:
            await server.open_agent(bad)
    assert raised.value.code == -32602


async def test_resuming_an_unknown_run_ends_with_not_found() -> None:
    async with Server() as server:
        agent = await server.open_agent(config([]))
        run = await agent.resume("no-such-run")
        with pytest.raises(RunNotFound):
            await run.result()


async def test_closing_the_server_stops_its_process() -> None:
    async with Server() as server:
        pid = server.pid
        await server.open_agent(config([]))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_the_server_reads_keys_only_from_the_env_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fake key; opening builds the client offline and no run is started.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-p11e-not-a-real-key")
    deepseek: AgentConfig = {
        "namespace": "p12",
        "spec": {"model": "deepseek-chat"},
        "provider": {"id": "deepseek"},
    }
    async with Server() as inheriting:
        await inheriting.open_agent(deepseek)
    async with Server(env={"DEEPSEEK_API_KEY": "sk-p11e-given"}) as given:
        await given.open_agent(deepseek)
    async with Server(env={}) as bare:
        with pytest.raises(OpenError) as raised:
            await bare.open_agent(deepseek)
    assert raised.value.code == -32013
    assert raised.value.data == {"env": "DEEPSEEK_API_KEY"}
