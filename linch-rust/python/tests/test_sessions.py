"""Sessions: a run id the client chooses, one live run per session, and closing a session."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripted import config, text_turn, tool_turn

from linch_rust import (
    InitEvent,
    ResultEvent,
    RunError,
    Server,
    SessionBusy,
    SuspendedEvent,
    ToolCallEndEvent,
    tool,
)


async def test_a_run_keeps_the_id_the_client_chose() -> None:
    async with Server() as server:
        agent = await server.open_agent(config([text_turn("Chosen.")]))
        run = await agent.run("s1", "Go", run_id="chosen-1")
        envelopes = [envelope async for envelope in run]

    assert run.id == "chosen-1"
    assert all(envelope.run_id == "chosen-1" for envelope in envelopes)
    init = envelopes[0].event
    assert isinstance(init, InitEvent) and init.run_id == "chosen-1"
    assert isinstance(envelopes[-1].event, ResultEvent)


async def test_a_second_live_run_on_a_session_is_session_busy() -> None:
    @tool(scope="write")
    async def save(text: str) -> str:
        return "saved"

    turns = [tool_turn("call_1", "save", {"text": "hello"}), text_turn("Done.")]
    async with Server() as server:
        agent = await server.open_agent(config(turns, mode="default"), tools=[save])
        waiting = await agent.run("s1", "Save hello")
        assert isinstance(await waiting.result(), SuspendedEvent)
        second = await agent.run("s1", "Something else")
        with pytest.raises(SessionBusy) as raised:
            await second.result()

    busy = raised.value
    assert isinstance(busy, RunError) and busy.code == -32030
    assert (busy.session_id, busy.run_id) == ("s1", waiting.id)


async def test_closing_a_session_forgets_what_it_read(tmp_path: Path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("alpha\n")
    turns = [
        tool_turn("call_1", "Read", {"file_path": "notes.txt"}),
        text_turn("Read it."),
        tool_turn("call_2", "Write", {"file_path": "notes.txt", "content": "beta\n"}),
        text_turn("Tried."),
    ]
    workspace = config(turns, mode="accept_edits", workspace=str(tmp_path))
    async with Server() as server:
        agent = await server.open_agent(workspace)
        read = await agent.run("s1", "Read notes.txt")
        assert isinstance(await read.result(), ResultEvent)
        await agent.close_session("s1")
        write = await agent.run("s1", "Overwrite notes.txt")
        envelopes = [envelope async for envelope in write]

    [end] = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
    assert end.tool_name == "Write" and end.is_error
    assert "Read notes.txt before changing it" in str(end.result)
    assert notes.read_text() == "alpha\n"
