"""Memory: compaction and context-length recovery, truncation recovery, and
external memory through `context_build`, against the real server."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from scripted import MODEL, config, text_turn, tool_turn

from linch_rust import (
    DETAILED_COMPACTION_PROMPT,
    Compaction,
    CompactionEvent,
    ContextBuildEvent,
    Envelope,
    ErrorEvent,
    ResultEvent,
    Server,
    TruncationRecovery,
    callback,
)
from linch_rust.memory import Chunk, latest_question, render, retrieval

COMPACTION_RS = Path(__file__).resolve().parents[2] / "crates/linch-core/src/compaction.rs"
TRUNCATION_RS = Path(__file__).resolve().parents[2] / "crates/linch-core/src/truncation.rs"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples/external_memory.py"


def refusal() -> dict[str, Any]:
    """A scripted call the provider refuses as longer than its context window."""
    return {
        "error": {
            "type": "context_length",
            "status": 400,
            "message": "prompt is too long: 201838 tokens > 200000 maximum",
        }
    }


def cut(text: str) -> dict[str, Any]:
    """A scripted text answer the output limit cut off."""
    turn = text_turn(text)
    turn["events"][-1]["stop_reason"] = "max_tokens"
    return turn


async def run_once(cfg: Any, prompt: str = "go") -> list[Envelope]:
    async with Server() as server:
        agent = await server.open_agent(cfg)
        run = await agent.run("s1", prompt)
        return [envelope async for envelope in run]


def final(envelopes: list[Envelope]) -> ResultEvent:
    result = envelopes[-1].event
    assert isinstance(result, ResultEvent)
    return result


def tool_calls(n: int) -> list[dict[str, Any]]:
    """`n` calls of a tool the agent does not have: each gets an error result.

    The input is long enough that a summary of the folded calls is smaller
    than they are (a summary that does not shorten the view is rejected).
    """
    return [tool_turn(f"call_{i}", "lookup", {"key": i, "note": "x" * 400}) for i in range(n)]


async def test_a_refused_prompt_is_summarized_and_called_again() -> None:
    compaction: Compaction = {"keep_recent_turns": 1}
    turns = [*tool_calls(2), refusal(), text_turn("the summary"), text_turn("done")]
    cfg = config(turns, mode="skip_dangerous")
    cfg["spec"] = {"model": MODEL, "compaction": compaction}
    envelopes = await run_once(cfg)

    [compacted] = [e.event for e in envelopes if isinstance(e.event, CompactionEvent)]
    assert (compacted.strategy, compacted.forced) == ("summary", True)
    assert final(envelopes).final_text == "done"
    assert not [e for e in envelopes if e.event.type == "provider_retry"]


async def test_without_compaction_a_refused_prompt_ends_the_run() -> None:
    envelopes = await run_once(config([refusal(), text_turn("never")]))

    [error] = [e.event for e in envelopes if isinstance(e.event, ErrorEvent)]
    assert (error.error["kind"], error.error["retryable"]) == ("context_length", False)
    assert final(envelopes).subtype == "error"


async def test_a_cut_answer_is_continued_and_stitched() -> None:
    recovery: TruncationRecovery = {"max_attempts": 2}
    cfg = config([cut("Hel"), cut("lo, "), text_turn("world")])
    cfg["spec"] = {"model": MODEL, "truncation_recovery": recovery}
    envelopes = await run_once(cfg)

    assert final(envelopes).final_text == "Hello, world"
    assert final(envelopes).stop_reason == "end_turn"


async def test_a_ladder_that_cannot_run_fails_the_open() -> None:
    from linch_rust import OpenError

    for spec in [
        {"model": MODEL, "truncation_recovery": {"max_attempts": 0}},
        {"model": MODEL, "compaction": {"keep_recent_turns": 0}},
    ]:
        cfg = config([])
        cfg["spec"] = spec
        async with Server() as server:
            with pytest.raises(OpenError):
                await server.open_agent(cfg)


@pytest.mark.source
def test_the_typed_specs_name_every_field_the_server_reads() -> None:
    for path, name, typed in [
        (COMPACTION_RS, "CompactionSpec", Compaction),
        (TRUNCATION_RS, "TruncationRecovery", TruncationRecovery),
    ]:
        source = path.read_text(encoding="utf-8")
        body = source.split(f"pub struct {name} {{", 1)[1].split("\n}", 1)[0]
        fields = set(re.findall(r"^\s*pub (\w+):", body, re.MULTILINE))
        assert fields == typed.__optional_keys__, name


@pytest.mark.source
def test_the_detailed_prompt_is_the_servers() -> None:
    source = COMPACTION_RS.read_text(encoding="utf-8")
    body = source.split('pub const DETAILED_PROMPT: &str = "', 1)[1].split('";', 1)[0]
    # A Rust string: `\` at a line end drops the break and the next indent.
    text = re.sub(r"\\\n\s*", "", body).replace("\\n", "\n")
    assert text == DETAILED_COMPACTION_PROMPT


async def test_a_detailed_compaction_folds_the_view() -> None:
    compaction: Compaction = {
        "threshold_tokens": 1,
        "keep_recent_turns": 1,
        "prompt": DETAILED_COMPACTION_PROMPT,
        "max_output_tokens": 8192,
    }
    sent: list[dict[str, Any]] = []

    @callback("before_provider_call", "spy", 1)
    async def spy(request: dict[str, Any]) -> dict[str, Any]:
        sent.append(request["request"])
        return {"action": "continue"}

    turns = [*tool_calls(2), text_turn("the summary"), text_turn("done")]
    cfg = config(turns, mode="skip_dangerous")
    cfg["spec"] = {"model": MODEL, "compaction": compaction}
    async with Server() as server:
        agent = await server.open_agent(cfg, callbacks=[spy])
        envelopes = [e async for e in await agent.run("s1", "go")]

    [compacted] = [e.event for e in envelopes if isinstance(e.event, CompactionEvent)]
    assert (compacted.strategy, compacted.forced) == ("summary", False)
    assert final(envelopes).final_text == "done"
    # The summary call is not a primary call: no hook sees it, so the spy
    # sees the three primary requests, the last built on the summary.
    assert len(sent) == 3
    assert any("the summary" in str(message) for message in sent[2]["messages"])


def test_the_question_skips_tool_results_and_reminders() -> None:
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "When do deploys happen?"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Looking."}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c", "content": []}]},
        {"role": "user", "content": [{"type": "text", "text": "<system-reminder>\nx"}]},
    ]
    assert latest_question(messages) == "When do deploys happen?"
    assert latest_question([]) == ""
    assert render(Chunk("a.md", "/v/a.md", "text")) == "[doc:a.md]\nSource: /v/a.md\ntext"


class Store:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks, self.queries = chunks, []

    async def retrieve(self, query: str, *, k: int) -> list[Chunk]:
        self.queries.append(query)
        return self.chunks[:k]


async def test_retrieval_adds_chunks_to_the_request_and_names_them_on_the_event() -> None:
    store = Store([Chunk("a", "notes/a.md", "alpha"), Chunk("b", "notes/b.md", "beta")])
    seen: list[dict[str, Any]] = []

    @callback("before_provider_call", "spy", 1)
    async def spy(request: dict[str, Any]) -> dict[str, Any]:
        seen.append(request["request"])
        return {"action": "continue"}

    async with Server() as server:
        agent = await server.open_agent(
            config([text_turn("ok")]), callbacks=[retrieval(store, k=1), spy]
        )
        run = await agent.run("s1", "what is alpha?")
        envelopes = [e async for e in run]

    assert store.queries == ["what is alpha?"]
    [build] = [e.event for e in envelopes if isinstance(e.event, ContextBuildEvent)]
    assert build.metadata == {"memory": {"ids": ["a"], "sources": ["notes/a.md"]}}
    last = seen[0]["messages"][-1]
    assert [b["text"] for b in last["content"]] == ["what is alpha?", render(store.chunks[0])]


def load_example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("external_memory", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def texts(message: dict[str, Any]) -> list[str]:
    return [str(block.get("text", "")) for block in message["content"]]


@pytest.mark.source
async def test_the_external_memory_example_remembers_and_recalls(tmp_path: Path) -> None:
    example = load_example()
    requests: list[list[dict[str, Any]]] = []

    @callback("before_provider_call", "spy", 1)
    async def spy(request: dict[str, Any]) -> dict[str, Any]:
        requests.append(request["request"]["messages"])
        return {"action": "continue"}

    first, second = await example.main(tmp_path, extra=[spy])

    assert (tmp_path / "deploy-day.md").read_text() == "Deploys happen on Tuesdays."
    assert final(first).final_text == "Noted, and the team is called Linch."
    assert final(second).final_text == "On Tuesdays."
    # Run 1 recalled the team note; run 2, the note run 1 saved.
    assert any("[doc:team.md]" in text for text in texts(requests[0][-1]))
    recalled = requests[-1]
    assert any("Deploys happen on Tuesdays." in t for t in texts(recalled[-1]))
    # What was recalled rode only its own request: none of it was logged,
    # so the history the second run sends holds no memory block.
    history = [text for message in recalled[:-1] for text in texts(message)]
    assert not [text for text in history if text.startswith("[doc:")], history
    # Nor is it in any durable event: the logged messages are the session's.
    logged = [repr(e.event) for run in (first, second) for e in run]
    assert not [event for event in logged if "[doc:" in event]
    assert any("Deploys happen on Tuesdays." in event for event in logged)
