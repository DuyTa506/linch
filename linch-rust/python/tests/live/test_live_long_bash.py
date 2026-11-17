"""Live e2e (area long): a long background command and the idle bound (P1-P3 of `long_results/expectations.md`)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from e2e_long_support import dump
from live_core_support import (
    ONLY_VLLM,
    Live,
    drive,
    live_server,
    make_config,
    text_of,
)

from linch_rust import LoopGuardEvent

pytestmark = pytest.mark.live

LIVE: Live = ONLY_VLLM[0].values[0]  # type: ignore[assignment]
RUNNER = (
    "You are a command runner. Follow the instructions exactly, one tool call at a time, "
    "and never run any command other than the ones named."
)


def alive(pid: int) -> bool:
    try:
        Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    stat = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
    return stat != "Z"


def bash_config(workspace: Path, **spec: Any) -> Any:
    return make_config(
        LIVE,
        LIVE.spec(system_prompt=RUNNER, **spec),
        workspace=str(workspace),
        permissions={"mode": "skip_dangerous", "project_root": str(workspace)},
    )


async def test_a_20_second_background_command_is_polled_to_its_end(tmp_path: Path) -> None:
    """P1 P3: the run does not end early; the process is gone once the session closes."""
    out: dict[str, Any] = {}
    async with live_server() as server:
        agent = await server.open_agent(bash_config(tmp_path.resolve(), max_turns=14))
        started = time.time()
        run = await agent.run(
            "p1",
            "Call Bash with command `sh -c 'sleep 20; echo done-20'` and run_in_background true. "
            "Then call BashOutput with the returned process_id and wait_ms 8000. Keep calling "
            "BashOutput with wait_ms 8000 until its result says the process ended. "
            "Then reply with the command's output.",
        )
        trace = await drive(agent, run, timeout=240)
        elapsed = time.time() - started
        start = trace.ends("Bash")[0]
        pid = start.metadata["pid"]
        outputs = [text_of(o) for o in trace.ends("BashOutput")]
        out.update(
            elapsed_s=round(elapsed, 1),
            bash_output_calls=len(outputs),
            outputs=outputs,
            loop_guard=[e.reason for e in trace.of(LoopGuardEvent)],
            subtype=trace.result.subtype,
            final_text=trace.result.final_text,
            alive_after_run=alive(pid),
        )
        await agent.close_session("p1")
        out["alive_after_close_session"] = alive(pid)
    out["alive_after_server_exit"] = alive(pid)
    dump("bash_long_poll", out)
    assert out["loop_guard"] == [], out
    assert out["subtype"] == "success" and out["elapsed_s"] >= 19, out
    assert any("done-20" in o for o in outputs), outputs
    assert out["alive_after_run"] is False  # it had ended by itself
    assert out["alive_after_close_session"] is False and out["alive_after_server_exit"] is False


async def test_a_silent_command_polled_without_waiting_trips_the_idle_bound(tmp_path: Path) -> None:
    """P2: the same answer `max_idle_turns` times in a row ends the run; the process outlives the run
    (documented) and dies with the session."""
    out: dict[str, Any] = {}
    async with live_server() as server:
        config = bash_config(
            tmp_path.resolve(), max_turns=24, loop_guard={"max_idle_turns": 3}
        )
        agent = await server.open_agent(config)
        run = await agent.run(
            "p2",
            "Call Bash with command `sleep 120` and run_in_background true. Then call BashOutput "
            "with the returned process_id, and no wait_ms, again and again (at least 10 times) "
            "until the process ended. Never stop early.",
        )
        trace = await drive(agent, run, timeout=240)
        start = trace.ends("Bash")[0]
        pid = start.metadata["pid"]
        out.update(
            bash_output_calls=len(trace.ends("BashOutput")),
            loop_guard=[(e.reason, e.detail) for e in trace.of(LoopGuardEvent)],
            subtype=trace.result.subtype,
            alive_after_run=alive(pid),
        )
        await agent.close_session("p2")
        out["alive_after_close_session"] = alive(pid)
    dump("bash_idle_bound", out)
    assert out["loop_guard"], out
    assert out["subtype"] == "error"
    assert out["alive_after_close_session"] is False
