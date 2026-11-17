"""Live toolsec: a stdio MCP server end to end (tools, errors, `polls`, lifeline, tenant allowlist).

Expectations are in `expectations/toolsec.md` section 4 (ids 4.1 to 4.6).
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from ts_support import (
    MCP_SERVER,
    alive,
    ends,
    fake_config,
    finish,
    needs_server,
    plan_model,
    pytestmark,
    record,
    result_of,
    server_env,
    serving,
    tenant,
    text_of,
    until_async,
    workspace,
)

from linch_rust import (
    InitEvent,
    LoopGuardEvent,
    NotAllowed,
    PermissionRequestEvent,
    Server,
)

__all__ = ["pytestmark"]

AREA = "mcp"


def mcp_entry(pid_file: Path | None = None, **extra: Any) -> dict[str, Any]:
    env = {"MCP_PID_FILE": str(pid_file)} if pid_file else {}
    env.update(extra.pop("env", {}))
    return {
        "name": "ts",
        "transport": {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-I", str(MCP_SERVER)],
            "env": env,
        },
        **extra,
    }


def call(tool: str, **args: object) -> tuple[str, dict[str, object]]:
    return (f"mcp__ts__{tool}", dict(args))


@needs_server
async def test_tools_list_call_scope_and_error() -> None:
    steps = [call("echo", text="hello-mcp"), call("add", a=19, b=23), call("fail"), call("pid")]
    model = await plan_model(steps).start()
    try:
        with workspace() as root:
            pid_file = root / "mcp.pid"
            async with Server(env=server_env()) as server:
                config = fake_config(
                    model,
                    mcp_servers=[
                        mcp_entry(pid_file, trust_annotations=True, call_timeout_ms=20000)
                    ],
                )
                agent = await server.open_agent(config)
                envelopes = await finish(agent, await agent.run("s1", "go"))
                server_pid = int(pid_file.read_text())
                running_between_runs = alive(server_pid)
                await agent.close()
                gone = await until_async(lambda: not alive(server_pid), 10)
    finally:
        await model.stop()
    init = [e.event for e in envelopes if isinstance(e.event, InitEvent)][0]
    asks = [e.event for e in envelopes if isinstance(e.event, PermissionRequestEvent)]
    asked = sorted({r["tool_name"] for a in asks for r in a.requests})
    done = {e.tool_name: e for e in ends(envelopes)}
    record(
        AREA,
        "4.1",
        tools=sorted(init.tools),
        asked_approval_for=asked,
        echo=text_of(done["mcp__ts__echo"]),
        add=text_of(done["mcp__ts__add"]),
        fail_is_error=done["mcp__ts__fail"].is_error,
        fail_text=text_of(done["mcp__ts__fail"]),
        child_dead_after_agent_close_s=gone,
        child_alive_between_runs=running_between_runs,
    )
    expected = {f"mcp__ts__{t}" for t in ("echo", "add", "fail", "poll", "pid")}
    assert expected <= set(init.tools)
    assert text_of(done["mcp__ts__echo"]) == "hello-mcp" and text_of(done["mcp__ts__add"]) == "42"
    # default permission mode: a read-only annotated tool (trusted) runs; the others ask
    assert "mcp__ts__echo" not in asked and "mcp__ts__add" in asked
    assert done["mcp__ts__fail"].is_error and "deliberate failure" in text_of(done["mcp__ts__fail"])
    assert result_of(envelopes).subtype == "success"
    assert running_between_runs and gone is not None


@needs_server
async def test_polls_flag_against_the_loop_guard() -> None:
    outcomes: dict[str, Any] = {}
    for label, polls, calls in (
        ("no_polls", [], 12),
        ("polls_12", ["poll"], 12),
        ("polls_40", ["poll"], 40),
    ):
        steps = [call("poll") for _ in range(calls)]
        model = await plan_model(steps).start()
        try:
            async with Server(env=server_env()) as server:
                agent = await server.open_agent(
                    fake_config(
                        model,
                        spec={"max_turns": 80},
                        mcp_servers=[mcp_entry(polls=polls)],
                        permissions={"mode": "skip_dangerous", "project_root": "/tmp"},
                    )
                )
                envelopes = await finish(agent, await agent.run("s1", "go"))
        finally:
            await model.stop()
        guards = [e.event for e in envelopes if isinstance(e.event, LoopGuardEvent)]
        outcomes[label] = {
            "subtype": result_of(envelopes).subtype,
            "loop_guard": [g.reason for g in guards],
            "calls_made": len(ends(envelopes, "mcp__ts__poll")),
        }
    record(AREA, "4.3", **outcomes)
    assert outcomes["no_polls"]["loop_guard"] and outcomes["no_polls"]["calls_made"] <= 4
    assert not outcomes["polls_12"]["loop_guard"] and outcomes["polls_12"]["subtype"] == "success"
    assert outcomes["polls_40"]["loop_guard"], "an endless poll with identical answers must end"


@needs_server
async def test_server_sigkill_reaps_a_stubborn_mcp_child_and_tenant_allowlist() -> None:
    model = await plan_model([call("echo", text="x")]).start()
    try:
        with workspace() as root:
            pid_file = root / "mcp.pid"
            entry_env = ["MCP_PID_FILE", "MCP_STUBBORN"]
            tenants = {
                "acme": tenant(
                    "tok-acme-0001",
                    permission_modes=["default", "skip_dangerous"],
                    provider_base_urls=[model.url],
                    mcp_commands=[
                        {
                            "command": sys.executable,
                            "args": ["-I", str(MCP_SERVER)],
                            "env": entry_env,
                        }
                    ],
                )
            }
            async with serving(tenants) as proc:
                async with proc.connect("tok-acme-0001") as client:
                    # allowlist refusals (nothing may start)
                    bad_command = fake_config(model, namespace="acme")
                    bad_command["mcp_servers"] = [
                        {
                            "name": "ts",
                            "transport": {"type": "stdio", "command": "/bin/echo", "args": []},
                        }
                    ]
                    with pytest.raises(NotAllowed) as e1:
                        await client.open_agent(bad_command)
                    bad_env = fake_config(model, namespace="acme")
                    bad_env["mcp_servers"] = [
                        mcp_entry(pid_file, env={"LD_PRELOAD": "/tmp/x.so", "MCP_STUBBORN": "1"})
                    ]
                    with pytest.raises(NotAllowed) as e2:
                        await client.open_agent(bad_env)
                    record(
                        AREA,
                        "4.6-refusals",
                        command=(e1.value.kind, str(e1.value)[:200]),
                        env=(e2.value.kind, str(e2.value)[:200]),
                    )
                    assert e1.value.kind == "mcp_command" and e2.value.kind == "mcp_env"
                    assert not pid_file.exists(), "a refused open started the MCP server"
                    # allowed: the stubborn child (ignores EOF and SIGTERM) runs
                    good = fake_config(model, namespace="acme")
                    good["mcp_servers"] = [mcp_entry(pid_file, env={"MCP_STUBBORN": "1"})]
                    agent = await client.open_agent(good)
                    child = int(pid_file.read_text())
                    assert alive(child)
                    envelopes = await finish(agent, await agent.run("s1", "go"))
                    assert ends(envelopes, "mcp__ts__echo")
                    start = time.monotonic()
                    proc.kill()
                    gone = await until_async(lambda: not alive(child), 10, every=0.05)
                    record(
                        AREA,
                        "4.5",
                        child=child,
                        dead_after_server_sigkill_s=gone,
                        wall=round(time.monotonic() - start, 2),
                    )
                    if alive(child):
                        import os
                        import signal

                        os.kill(child, signal.SIGKILL)
                    assert gone is not None and gone <= 6, gone
    finally:
        await model.stop()


@needs_server
async def test_a_server_config_listing_path_in_mcp_env_does_not_start() -> None:
    import json
    import subprocess

    from linch_rust import find_server

    with tempfile.TemporaryDirectory(prefix="ts-cfg-") as directory:
        cfg = {
            "socket": f"{directory}/s.sock",
            "tenants": {
                "acme": tenant(
                    "tok-acme-0001",
                    mcp_commands=[{"command": "/bin/echo", "args": [], "env": ["PATH"]}],
                )
            },
        }
        path = Path(directory) / "c.json"
        path.write_text(json.dumps(cfg))
        done = subprocess.run(
            [find_server(), "--config", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            env=server_env(),
        )
    record(AREA, "4.6-config-path", code=done.returncode, stderr=done.stderr[:300])
    assert done.returncode == 2 and "PATH" in done.stderr
