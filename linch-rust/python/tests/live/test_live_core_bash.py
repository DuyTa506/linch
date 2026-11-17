"""Live: the Bash tool: foreground, background, kill, timeout, truncation, environment."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
from live_core_support import (
    CANARY,
    PROVIDERS,
    Live,
    drive,
    live_server,
    make_config,
    retry_once,
    text_of,
)

pytestmark = pytest.mark.live

RUNNER = (
    "You are a command runner. When told to run a command, call the Bash tool with exactly "
    "that command (keep every quote), and do not run anything else. Then reply DONE."
)


def bash_config(live: Live, workspace: Path, **top: Any) -> Any:
    top.setdefault("permissions", {"mode": "skip_dangerous", "project_root": str(workspace)})
    spec = live.spec(system_prompt=RUNNER, max_turns=6)
    return make_config(live, spec, workspace=str(workspace), **top)


async def run_commands(live: Live, workspace: Path, prompt: str, **top: Any) -> Any:
    async with live_server() as server:
        agent = await server.open_agent(bash_config(live, workspace, **top))
        return await drive(agent, await agent.run("s1", prompt), timeout=240)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return stat.exists() and stat.read_text().split(")")[-1].split()[0] != "Z"


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_bash_runs_in_the_workspace(live: Live, tmp_path: Path) -> None:
    workspace = tmp_path.resolve()
    trace = await run_commands(
        live, workspace, "Run this command: echo hello > out.txt && pwd && echo second-line"
    )
    [end] = trace.ends("Bash")
    assert not end.is_error
    assert (workspace / "out.txt").read_text() == "hello\n"
    text = text_of(end)
    assert str(workspace) in text and "second-line" in text and "[exit code 0]" in text


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_failing_command_is_an_error_result_with_its_exit_status(
    live: Live, tmp_path: Path
) -> None:
    trace = await run_commands(live, tmp_path.resolve(), "Run this command: sh -c 'exit 7'")
    [end] = trace.ends("Bash")
    assert end.is_error and end.error_code == "exit_status" and "7" in text_of(end)
    assert trace.result.subtype == "success"


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_background_command_is_read_with_bashoutput(live: Live, tmp_path: Path) -> None:
    trace = await run_commands(
        live,
        tmp_path.resolve(),
        "Call Bash with command `sh -c 'sleep 1; echo bg-finished-77'` and run_in_background "
        "true. Then call BashOutput with the returned process_id and wait_ms 8000.",
    )
    [start] = trace.ends("Bash")
    assert "BashOutput" in text_of(start) and start.metadata
    process_id = start.metadata["process_id"]
    assert re.fullmatch(r"bash_[0-9a-f]{12}", process_id)
    outputs = trace.ends("BashOutput")
    assert any("bg-finished-77" in text_of(o) for o in outputs), [text_of(o) for o in outputs]
    assert not alive(start.metadata["pid"])


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_bashkill_stops_a_background_command(live: Live, tmp_path: Path) -> None:
    trace = await run_commands(
        live,
        tmp_path.resolve(),
        "Call Bash with command `sleep 300` and run_in_background true. Then call BashKill "
        "with the returned process_id.",
    )
    [start] = trace.ends("Bash")
    assert start.metadata and "pid" in start.metadata
    assert trace.ends("BashKill") and not trace.ends("BashKill")[0].is_error
    assert not alive(start.metadata["pid"])


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_command_past_its_timeout_is_killed(live: Live, tmp_path: Path) -> None:
    trace = await run_commands(
        live,
        tmp_path.resolve(),
        "Call Bash with command `sleep 60` and timeout_ms 1500.",
    )
    [end] = trace.ends("Bash")
    assert end.is_error and end.error_code == "timeout"
    assert end.duration_ms < 20_000


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_huge_output_is_truncated_to_head_and_tail(live: Live, tmp_path: Path) -> None:
    trace = await run_commands(
        live,
        tmp_path.resolve(),
        "Run this command: python3 -c \"print('H' * 3000 + 'M' * 200000 + 'T' * 3000)\"",
    )
    end = trace.ends("Bash")[0]
    text = text_of(end)
    # Head 4000 and tail 26000 bytes of the stream (docs/architecture/tools.md).
    assert 25_000 < len(text) < 40_000
    assert text.startswith("H" * 100) and "T" * 100 in text
    assert text.count("M") < 200_000 and "bytes omitted" in text


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_the_servers_secrets_do_not_reach_the_command(live: Live, tmp_path: Path) -> None:
    workspace = tmp_path.resolve()
    async with live_server() as server:
        agent = await server.open_agent(bash_config(live, workspace))
        prompt = (
            "Run this command: env; echo ---; "
            f"tr '\\0' '\\n' < /proc/{server.pid}/environ; echo ---; pwd"
        )
        trace = await drive(agent, await agent.run("s1", prompt), timeout=240)
    [end] = trace.ends("Bash")
    text = text_of(end)
    assert CANARY not in text and "LIVECORE_CANARY" not in text
    assert "DEEPSEEK_API_KEY" not in text and "sk-" not in text
    assert "PATH=" in text and str(workspace) in text
    assert trace.result.subtype == "success"
