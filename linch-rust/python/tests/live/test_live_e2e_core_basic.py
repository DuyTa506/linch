"""Live e2e (core): a basic run, usage and cost, the tool loop, built-in read tools, parallelism."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from e2e_core_support import (
    MODEL,
    attempts,
    config,
    dump,
    fwd,  # noqa: F401  (fixture)
    needs_e2e,
    opened,
    run_to_end,
    spec,
)
from live_core_support import PG_DSN, message_text, text_of

from linch_rust import InitEvent, PartialAssistantEvent, UsageEvent, tool

__all__ = ["fwd"]

pytestmark = [pytest.mark.live, needs_e2e]


def run_status(namespace: str, run_id: str) -> str:
    import psycopg

    assert PG_DSN
    with psycopg.connect(PG_DSN) as conn:
        row = conn.execute(
            "SELECT status FROM linch_runs WHERE namespace = %s AND run_id = %s",
            (namespace, run_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


@attempts("1a/1b basic run, usage, no pricing")
async def test_1_basic_run_events_usage_and_a_durable_row(fwd: Any) -> None:
    cfg = config(fwd)
    async with opened(cfg) as (_server, agent):
        trace = await run_to_end("1-basic", agent, "Reply with exactly the word: pong", fwd=fwd)
        [init] = trace.of(InitEvent)
        types = [e.type for e in trace.events]
        wire = fwd.chat()[-1]
        status = run_status(cfg["namespace"], init.run_id)
    result = trace.result
    usages = trace.of(UsageEvent)
    dump(
        "1-basic",
        {
            "types": types,
            "status": status,
            "total_usage": result.total_usage,
            "wire_usage": wire["usage"],
        },
    )
    assert types[0] == "init" and types[-1] == "result" and "assistant" in types
    assert init.provider == "vllm" and init.model == MODEL
    assert result.subtype == "success" and result.usage_complete
    assert "pong" in (result.final_text or "").lower()
    assert len(usages) == 1
    out = result.total_usage["output_tokens"]
    assert out == usages[0].usage["output_tokens"] == wire["usage"]["completion_tokens"]
    assert (
        result.total_usage["input_tokens"] + result.total_usage.get("cache_read_tokens", 0)
        == (wire["usage"]["prompt_tokens"])
    )
    assert result.total_cost_usd is None and usages[0].cost_usd is None
    assert status in {"completed", "done", "success"}, status
    streamed = "".join(
        d.delta.get("text", "")
        for d in trace.of(PartialAssistantEvent)
        if d.delta["kind"] == "text"
    )
    assert streamed == message_text(trace.assistants()[0].message)


@attempts("1c cost with pricing")
async def test_1c_cost_equals_tokens_times_price(fwd: Any) -> None:
    p_in, p_out = 0.37, 1.91  # USD per million tokens
    cfg = config(fwd, pricing={MODEL: {"input": p_in, "output": p_out}})
    async with opened(cfg, tools=()) as (_server, agent):
        trace = await run_to_end("1c-cost", agent, "Reply with exactly the word: pong", fwd=fwd)
        second = await run_to_end("1c-cost", agent, "Say OK twice.", session="s2", fwd=fwd)
    for t in (trace, second):
        u = t.result.total_usage
        expected = (
            (u["input_tokens"] + u.get("cache_read_tokens", 0) + u.get("cache_creation_tokens", 0))
            * p_in
            + u["output_tokens"] * p_out
        ) / 1e6
        usages = t.of(UsageEvent)
        dump("1c-cost", {"usage": u, "expected": expected, "reported": t.result.total_cost_usd})
        assert t.result.total_cost_usd is not None
        assert abs(t.result.total_cost_usd - expected) < 1e-9, (t.result.total_cost_usd, expected)
        assert abs(sum(x.cost_usd or 0 for x in usages) - expected) < 1e-9
        assert abs((usages[-1].cumulative_cost_usd or 0) - expected) < 1e-9


@attempts("2a host tool loop")
async def test_2a_tool_loop_continues_while_tool_calls_and_stops_on_text(fwd: Any) -> None:
    seen: list[str] = []

    @tool(scope="read")
    async def get_secret(name: str) -> str:
        """Return the secret value registered under a name."""
        seen.append(name)
        return {"alpha": "TOKEN-7731", "beta": "TOKEN-2290"}.get(name, "unknown")

    cfg = config(fwd, spec(max_turns=8))
    async with opened(cfg, tools=(get_secret,)) as (_s, agent):
        trace = await run_to_end(
            "2a-tools",
            agent,
            "Call get_secret for name 'alpha', then for name 'beta' (one call per response, "
            "wait for each result), then state both values.",
            fwd=fwd,
        )
    calls = len(trace.starts("get_secret"))
    kinds = [e.type for e in trace.events]
    assert seen[:2] == ["alpha", "beta"] and calls == 2
    assert kinds.index("tool_call_start") < kinds.index("tool_call_end")
    assistants = trace.assistants()
    with_tools = [
        a for a in assistants if any(b["type"] == "tool_use" for b in a.message["content"])
    ]
    assert len(assistants) == len(with_tools) + 1  # the loop stops on the first text-only response
    assert all(b["type"] != "tool_use" for b in assistants[-1].message["content"])
    final = trace.result.final_text or ""
    assert "7731" in final and "2290" in final
    assert len(fwd.chat()) >= 3 and trace.result.subtype == "success"


@attempts("2b Read/Glob/Grep")
async def test_2b_builtin_read_tools_on_a_temp_dir(fwd: Any, tmp_path: Path) -> None:
    ws = tmp_path.resolve()
    (ws / "notes").mkdir()
    (ws / "notes" / "a.txt").write_text("nothing here\n")
    (ws / "notes" / "b.txt").write_text("line one\nsecret_code = ZX-4417\nline three\n")
    (ws / "notes" / "c.md").write_text("markdown, not matched by the glob\n")
    cfg = config(fwd, spec(max_turns=8), workspace=str(ws), bash=False)
    async with opened(cfg) as (_s, agent):
        trace = await run_to_end(
            "2b-builtin",
            agent,
            "Use Glob with pattern 'notes/*.txt' to list the files, then Grep for 'secret_code' "
            "in the notes directory, then Read the file that contains it. Report the code.",
            fwd=fwd,
        )
    names = [s.tool_name for s in trace.starts()]
    assert {"Glob", "Grep", "Read"} <= set(names), names
    assert not any(e.is_error for e in trace.ends()), [
        text_of(e) for e in trace.ends() if e.is_error
    ]
    assert "ZX-4417" in (trace.result.final_text or "")


@attempts("2c parallel reads, serial writes")
async def test_2c_read_tools_overlap_and_write_tools_serialise(fwd: Any, tmp_path: Path) -> None:
    import asyncio

    spans: dict[str, tuple[float, float]] = {}

    async def hold(name: str) -> str:
        start = time.monotonic()
        await asyncio.sleep(1.0)
        spans[name] = (start, time.monotonic())
        return f"{name}-done"

    @tool(scope="read")
    async def check_alpha() -> str:
        """Check the alpha service."""
        return await hold("alpha")

    @tool(scope="read")
    async def check_beta() -> str:
        """Check the beta service."""
        return await hold("beta")

    @tool(scope="write")
    async def save_one() -> str:
        """Save record one."""
        return await hold("save_one")

    @tool(scope="write")
    async def save_two() -> str:
        """Save record two."""
        return await hold("save_two")

    root = str(tmp_path.resolve())
    cfg = config(
        fwd,
        spec(max_turns=6),
        permissions={"mode": "skip_dangerous", "project_root": root},
    )
    async with opened(cfg, tools=(check_alpha, check_beta, save_one, save_two)) as (_s, agent):
        t0 = time.monotonic()
        reads = await run_to_end(
            "2c-reads",
            agent,
            "Call check_alpha and check_beta in the same response (two calls at once), then say "
            "done.",
            fwd=fwd,
        )
        read_wall = time.monotonic() - t0
        read_spans = dict(spans)
        spans.clear()
        writes = await run_to_end(
            "2c-writes",
            agent,
            "Call save_one and save_two in the same response (two calls at once), then say done.",
            session="s2",
            fwd=fwd,
        )
        write_spans = dict(spans)
    dump(
        "2c",
        {"read_spans": read_spans, "write_spans": write_spans, "read_wall_incl_model": read_wall},
    )
    first = [b["name"] for b in reads.assistants()[0].message["content"] if b["type"] == "tool_use"]
    assert first == ["check_alpha", "check_beta"], first
    (a0, a1), (b0, b1) = read_spans["alpha"], read_spans["beta"]
    assert max(a0, b0) < min(a1, b1), f"reads were serialised: {read_spans}"
    first_w = [
        b["name"] for b in writes.assistants()[0].message["content"] if b["type"] == "tool_use"
    ]
    assert first_w == ["save_one", "save_two"], first_w
    (c0, c1), (d0, d1) = write_spans["save_one"], write_spans["save_two"]
    assert c1 <= d0 + 0.02 or d1 <= c0 + 0.02, f"writes overlapped: {write_spans}"
