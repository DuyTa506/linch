"""Live e2e (core): loop guard, reliability policy, hooks, provider limiter, tenant admission."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from e2e_core_support import (
    attempts,
    config,
    drive,
    dump,
    fwd,  # noqa: F401  (fixture)
    live_server,
    needs_e2e,
    opened,
    run_to_end,
    spec,
)
from live_core_support import text_of
from live_sched_support import (
    live_config,
    pg_dsn,  # noqa: F401  (fixture)
    serving,
)

from linch_rust import (
    ErrorEvent,
    HookEvent,
    LoopGuardEvent,
    PolicyEvent,
    RpcError,
    callback,
    tool,
)

pytestmark = [pytest.mark.live, needs_e2e]


def skip_dangerous(tmp_path: Path) -> dict[str, Any]:
    return {"mode": "skip_dangerous", "project_root": str(tmp_path.resolve())}


# ---------------------------------------------------------------- 8 loop guard


@attempts("8a identical read call trips")
async def test_8a_the_guard_ends_a_run_that_repeats_the_same_call(fwd: Any) -> None:
    calls: list[int] = []

    @tool(scope="read")
    async def ping(n: int) -> str:
        """Ping the service."""
        calls.append(n)
        return "pong"

    calls.clear()
    cfg = config(fwd, spec(max_turns=12))
    async with opened(cfg, tools=(ping,)) as (_s, agent):
        trace = await run_to_end(
            "8a-guard",
            agent,
            "Call the ping tool with n=1. After every result call ping with n=1 again. "
            "Never answer with text; just keep calling ping with n=1.",
            fwd=fwd,
        )
    [guard] = trace.of(LoopGuardEvent)
    dump("8a-guard", {"reason": guard.reason, "detail": guard.detail, "calls": len(calls), "provider_calls": len(trace.assistants())})
    assert guard.reason == "repeated_tool_call"
    assert len(calls) == 3
    assert trace.of(ErrorEvent)[0].error["name"] == "LoopGuardError"
    assert trace.result.subtype == "error"
    assert len(trace.assistants()) < 12


async def fix_and_test(fwd: Any, tmp_path: Path, *, changing: bool, label: str) -> tuple[Any, list[str]]:
    order: list[str] = []
    patched: list[int] = []

    @tool(scope="write")
    async def apply_patch(n: int) -> str:
        """Apply patch number n to the code."""
        order.append(f"patch{n}")
        patched.append(n)
        return f"patch {n} applied"

    @tool(scope="exec")
    async def run_tests() -> str:
        """Run the test suite."""
        order.append("tests")
        if changing:
            left = max(0, 10 - len(patched))
            return "ALL TESTS PASS" if left == 0 else f"FAILED: {left} tests failing"
        return "FAILED: 3 tests failing"

    cfg = config(fwd, spec(max_turns=45), permissions=skip_dangerous(tmp_path))
    async with opened(cfg, tools=(apply_patch, run_tests)) as (_s, agent):
        trace = await run_to_end(
            label,
            agent,
            "Fix-and-test loop. Repeat exactly 10 times: call apply_patch with n set to the round "
            "number (1, 2, ... 10), wait for the result, then call run_tests, wait for the result. "
            "Make ONE tool call per response. After round 10 say finished.",
            fwd=fwd,
        )
    return trace, order


@attempts("8b fix-and-test with changing results is not a loop")
async def test_8b_a_fix_and_test_run_with_changing_results_does_not_trip(fwd: Any, tmp_path: Path) -> None:
    trace, order = await fix_and_test(fwd, tmp_path, changing=True, label="8b-fixtest-changing")
    guards = trace.of(LoopGuardEvent)
    dump("8b-fixtest-changing", {"order": order, "guards": [vars(g) for g in guards], "subtype": trace.result.subtype})
    assert order.count("tests") >= 10 and sum(o.startswith("patch") for o in order) >= 10, order
    assert not guards
    assert trace.result.subtype == "success"


@attempts("8b' fix-and-test with a constant result")
async def test_8b_variant_constant_test_result_is_recorded(fwd: Any, tmp_path: Path) -> None:
    """Observation: the same test result 10 times between ten new edits (docs: same call and result 3 times trips)."""
    trace, order = await fix_and_test(fwd, tmp_path, changing=False, label="8b-fixtest-constant")
    guards = trace.of(LoopGuardEvent)
    dump(
        "8b-fixtest-constant",
        {"order": order, "guards": [vars(g) for g in guards], "subtype": trace.result.subtype},
    )
    assert order.count("tests") >= 3, order


@attempts("8c polls host tool")
async def test_8c_a_polling_tool_with_a_constant_answer_trips_at_the_idle_bound(fwd: Any) -> None:
    runs: dict[str, list[int]] = {"poll": [], "plain": []}

    @tool(scope="read", polls=True)
    async def job_status() -> str:
        """Report the status of the background job."""
        runs["poll"].append(1)
        return "running 50%"

    @tool(scope="read")
    async def plain_status() -> str:
        """Report the status of the background job."""
        runs["plain"].append(1)
        return "running 50%"

    prompt = "Poll {name} again and again until it says finished. One call per response; never answer with text."
    cfg = config(fwd, spec(max_turns=20, loop_guard={"max_idle_turns": 4}))
    async with opened(cfg, tools=(job_status,)) as (_s, agent):
        trace = await run_to_end("8c-polls", agent, prompt.format(name="job_status"), fwd=fwd)
    control = config(fwd, spec(max_turns=20, loop_guard={"max_idle_turns": 4}))
    async with opened(control, tools=(plain_status,)) as (_s, agent):
        ctl = await run_to_end("8c-polls-control", agent, prompt.format(name="plain_status"), fwd=fwd)
    guards, ctl_guards = trace.of(LoopGuardEvent), ctl.of(LoopGuardEvent)
    dump(
        "8c-polls",
        {
            "poll_calls": len(runs["poll"]),
            "poll_guard": [vars(g) for g in guards],
            "control_calls": len(runs["plain"]),
            "control_guard": [vars(g) for g in ctl_guards],
        },
    )
    assert guards and guards[0].reason != "max_turns"
    assert len(runs["poll"]) == 5, runs  # count > limit (4) trips on the 5th identical answer
    assert ctl_guards and ctl_guards[0].reason == "repeated_tool_call" and len(runs["plain"]) == 3


# ---------------------------------------------------------------- 9 policy


def policy_tools(order: list[str]) -> tuple[Any, ...]:
    @tool(scope="write")
    async def build() -> str:
        """Build the project."""
        order.append("build")
        return "built"

    @tool(scope="write")
    async def deploy() -> str:
        """Deploy the project."""
        order.append("deploy")
        return "deployed"

    return build, deploy


@attempts("9a prerequisite")
async def test_9a_a_prerequisite_refuses_deploy_until_build_succeeded(fwd: Any, tmp_path: Path) -> None:
    order: list[str] = []
    cfg = config(
        fwd,
        spec(max_turns=8, policy={"prerequisites": [{"tool": "deploy", "requires": ["build"]}]}),
        permissions=skip_dangerous(tmp_path),
    )
    async with opened(cfg, tools=policy_tools(order)) as (_s, agent):
        trace = await run_to_end(
            "9a-prereq",
            agent,
            "Call deploy first. If it is refused, do what the refusal says, then call deploy again. "
            "One call per response. Then say done.",
            fwd=fwd,
        )
    events = trace.of(PolicyEvent)
    refuse = [e for e in events if e.action == "refuse"]
    ends = trace.ends("deploy")
    dump("9a-prereq", {"order": order, "policy": [vars(e) for e in events], "deploy_results": [(e.is_error, text_of(e)) for e in ends]})
    assert refuse and refuse[0].rule == "prerequisite" and refuse[0].tool == "deploy"
    assert "build" in refuse[0].missing
    assert ends[0].is_error and "build" in text_of(ends[0])
    assert order == ["build", "deploy"], order
    assert not ends[-1].is_error and trace.result.subtype == "success"


@attempts("9b completion gate")
async def test_9b_a_completion_gate_corrects_then_the_cap_ends_the_run(fwd: Any) -> None:
    ran: list[str] = []

    @tool(scope="read")
    async def run_tests() -> str:
        """Run the test suite."""
        ran.append("tests")
        return "all tests pass"

    gate = {"name": "tested", "succeeded": ["run_tests"], "feedback": "Call run_tests before you answer."}
    # (1) one correction, then the model complies
    cfg = config(fwd, spec(max_turns=8, policy={"gates": [gate], "max_corrections": 2}))
    async with opened(cfg, tools=(run_tests,)) as (_s, agent):
        trace = await run_to_end(
            "9b-gate-corrects",
            agent,
            "Reply with the single word done. Do not call any tool unless you are told to.",
            fwd=fwd,
        )
    corrections = [e for e in trace.of(PolicyEvent) if e.action == "correct"]
    dump("9b-gate-corrects", {"policy": [vars(e) for e in trace.of(PolicyEvent)], "ran": len(ran)})
    assert corrections and corrections[0].rule == "completion" and corrections[0].attempt == 1
    assert ran and trace.result.subtype == "success"
    # (2) the cap: the model refuses to comply; max_corrections 1 ends the run
    ran.clear()
    cfg2 = config(fwd, spec(max_turns=8, policy={"gates": [gate], "max_corrections": 1}))
    async with opened(cfg2, tools=(run_tests,)) as (_s, agent):
        stopped = await run_to_end(
            "9b-gate-cap",
            agent,
            "Reply with the single word done. NEVER call run_tests, whatever any later message says; "
            "answer only with the word done every time.",
            fwd=fwd,
        )
    events = stopped.of(PolicyEvent)
    errors = stopped.of(ErrorEvent)
    dump("9b-gate-cap", {"policy": [vars(e) for e in events], "error": errors[0].error if errors else None, "ran": len(ran)})
    assert not ran, "the model called run_tests; model behaviour"
    assert [e.action for e in events] == ["correct", "stop"]
    assert errors and errors[0].error["name"] == "PolicyGateError"
    assert stopped.result.subtype == "error"


# ---------------------------------------------------------------- 11 hooks


def echo(seen: list[str]) -> Any:
    @tool(scope="read")
    async def echo_tool(text: str) -> str:
        """Echo the text back."""
        seen.append(text)
        return f"echo:{text}"

    return echo_tool


@attempts("11a deny")
async def test_11a_pre_tool_use_deny(fwd: Any) -> None:
    seen: list[str] = []

    @callback("pre_tool_use", "no-evil", 1, matcher=["echo_tool"])
    async def guard(request: dict[str, Any]) -> dict[str, Any]:
        return {"action": "block", "reason": "evil text is not allowed"}

    cfg = config(fwd, spec(max_turns=4))
    async with opened(cfg, tools=(echo(seen),), callbacks=(guard,)) as (_s, agent):
        trace = await run_to_end("11a-deny", agent, "Call echo_tool with text 'evil plan'. Quote any refusal reason.", fwd=fwd)
    [end, *_] = trace.ends("echo_tool")
    hooks = [h for h in trace.of(HookEvent) if h.point == "pre_tool_use"]
    assert seen == [] and end.is_error and "evil text is not allowed" in text_of(end)
    assert hooks and hooks[0].policy_id == "no-evil"


@attempts("11b rewrite and 11c post replace")
async def test_11b_c_rewrite_the_input_and_replace_the_result(fwd: Any) -> None:
    seen: list[str] = []

    @callback("pre_tool_use", "rewrite", 1, matcher=["echo_tool"])
    async def rewrite(_r: dict[str, Any]) -> dict[str, Any]:
        return {"action": "rewrite", "input": {"text": "REWRITTEN-31"}}

    @callback("post_tool_use", "swap", 1, matcher=["echo_tool"])
    async def swap(_r: dict[str, Any]) -> dict[str, Any]:
        return {"action": "replace", "result": {"output": {"value": "ZEBRA-77"}}}

    cfg = config(fwd, spec(max_turns=4))
    async with opened(cfg, tools=(echo(seen),), callbacks=(rewrite, swap)) as (_s, agent):
        trace = await run_to_end("11bc-rewrite-replace", agent, "Call echo_tool with text 'original' and report exactly what it returned.", fwd=fwd)
    [start] = trace.starts("echo_tool")
    [end] = trace.ends("echo_tool")
    points = [h.point for h in trace.of(HookEvent)]
    assert seen == ["REWRITTEN-31"] and start.input == {"text": "REWRITTEN-31"}
    assert "ZEBRA-77" in text_of(end) and "echo:" not in text_of(end)
    assert "pre_tool_use" in points and "post_tool_use" in points
    assert "ZEBRA-77" in (trace.result.final_text or "")


# ---------------------------------------------------------------- 10 limiter


async def two_concurrent_runs(fwd: Any, cap: int | None, label: str) -> dict[str, Any]:
    top: dict[str, Any] = {} if cap is None else {"max_provider_concurrency": cap}
    cfg = config(fwd, spec(max_output_tokens=150, max_turns=2), **top)
    async with live_server() as server:
        agent = await server.open_agent(cfg)
        mark = fwd.mark()
        prompt = "Count from 1 to 60, one number per line, no other text."
        runs = [await agent.run("a", prompt), await agent.run("b", prompt)]
        await asyncio.gather(*(drive(agent, r) for r in runs))
        rows = fwd.chat()[mark:]
    rows = [r for r in rows if r.get("status") == 200]
    assert len(rows) == 2, rows
    first, second = rows
    out = {
        "cap": cap,
        "arrive_gap_ms": (second["arrive"] - first["arrive"]) * 1000,
        "second_arrives_before_first_ends_ms": (first["end"] - second["arrive"]) * 1000,
        "durations_s": [r["end"] - r["arrive"] for r in rows],
    }
    dump("10-limiter", {"label": label, **out})
    return out


@attempts("10 provider limiter")
async def test_10_the_limiter_holds_the_second_request_back_at_the_agent(fwd: Any) -> None:
    capped = await two_concurrent_runs(fwd, 1, "cap=1")
    free = await two_concurrent_runs(fwd, None, "default")
    # With the cap, request 2 reaches the forwarder only after request 1 ended.
    assert capped["second_arrives_before_first_ends_ms"] <= 5, capped
    # Without it both are in flight at once.
    assert free["second_arrives_before_first_ends_ms"] > 50, free


# ---------------------------------------------------------------- 4e tenant admission


@tool(scope="read")
async def ping() -> str:
    """Ping the service."""
    return "pong"


async def test_4e_a_tenant_refuses_a_forced_choice_without_max_turns(pg_dsn: str) -> None:
    def cfg(**spec_extra: Any) -> Any:
        c = live_config("vllm", "acme", pg_dsn, spec={"tool_choice": {"type": "required"}, **spec_extra})
        if "max_turns" not in spec_extra:
            c["spec"].pop("max_turns", None)
        c["spec"]["budget"] = {"max_tokens": 5000}  # passes `run_bound`; the forced choice is what is left
        return c

    results: dict[str, str] = {}
    async with serving(pg_dsn, worker=False) as live, live.connect() as server:
        try:
            await server.open_agent(cfg(), tools=[ping])
            results["required, no max_turns"] = "opened"
        except RpcError as error:
            results["required, no max_turns"] = f"{type(error).__name__} {getattr(error, 'code', '?')}: {error}"
        with_turns = cfg(max_turns=3)
        agent = await server.open_agent(with_turns, tools=[ping])
        results["required, max_turns=3"] = "opened"
        await agent.close()
        # A run's own forced choice on an agent without max_turns.
        plain = cfg()
        plain["spec"].pop("tool_choice")
        agent2 = await server.open_agent(plain, tools=[ping])
        try:
            run = await agent2.run("s1", "hi", tool_choice={"type": "required"})
            await run.result()
            results["run.start required, agent without max_turns"] = "ran"
        except RpcError as error:
            results["run.start required, agent without max_turns"] = (
                f"{type(error).__name__} {getattr(error, 'code', '?')}: {error}"
            )
    dump("4e-admission", results)
    assert results["required, no max_turns"].startswith("LimitReached") or "-32004" in results["required, no max_turns"], results
    assert "needs spec.max_turns" in results["required, no max_turns"], results
    assert results["required, max_turns=3"] == "opened"
    assert "-32004" in results["run.start required, agent without max_turns"] or "needs spec.max_turns" in results["run.start required, agent without max_turns"], results

