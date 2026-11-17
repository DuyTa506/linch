"""Live toolsec: remote execution (`SubprocessHost`, a recording host, tenants) on a real server.

Expectations are in `toolsec/expectations.md` section 3 (ids 3.1 to 3.7).
"""

from __future__ import annotations

import asyncio
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from live_sched_support import pg_dsn
from test_ts_bash import drop_client, wait_line
from ts_support import (
    FakeModel,
    descendants,
    ends,
    fake_config,
    finish,
    needs_server,
    plan_model,
    pytestmark,
    record,
    result_of,
    serving,
    skip_dangerous,
    tenant,
    text_of,
    until_async,
    workspace,
)

from linch_rust import (
    ExecStart,
    InitEvent,
    NotAllowed,
    RpcError,
    Server,
    SubprocessHost,
)

__all__ = ["pg_dsn", "pytestmark"]

AREA = "remote"
REMOTE: dict[str, Any] = {"backend": "remote"}
PID_RE = re.compile(r"bash_[0-9a-f]{12}")


def bash(command: str, **extra: object) -> tuple[str, dict[str, object]]:
    return ("Bash", {"command": command, **extra})


class Recording(SubprocessHost):
    """A host that records every `exec.start` it receives."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.starts: list[ExecStart] = []

    async def start(self, request: ExecStart) -> None:
        self.starts.append(request)
        await super().start(request)


def remote_config(model: Any, root: Path, **top: Any) -> Any:
    top.setdefault("execution", REMOTE)
    return fake_config(model, workspace=str(root), permissions=skip_dangerous(root), **top)


@needs_server
async def test_subprocess_host_runs_commands_times_out_and_serves_background() -> None:
    def output(text: str) -> tuple[str, dict[str, object]]:
        return ("BashOutput", {"process_id": PID_RE.search(text).group(0), "wait_ms": 6000})  # type: ignore[union-attr]

    steps: list[Any] = [
        bash("echo remote-hi; pwd"),
        bash("sleep 60", timeout_ms=1500),
        bash("sh -c 'sleep 1; echo bg-remote-done'", run_in_background=True),
        output,
    ]
    model = await plan_model(steps).start()
    try:
        with workspace() as root:
            host_dir = root / "hostcwd"
            host_dir.mkdir()
            async with Server() as server:
                agent = await server.open_agent(
                    remote_config(model, root), execution=SubprocessHost(cwd=host_dir)
                )
                start = time.monotonic()
                envelopes = await finish(agent, await agent.run("s1", "go"))
                elapsed = time.monotonic() - start
    finally:
        await model.stop()
    init = [e.event for e in envelopes if isinstance(e.event, InitEvent)][0]
    done = ends(envelopes, "Bash")
    record(AREA, "3.1", tools=init.tools, text=text_of(done[0]))
    assert sorted(init.tools) == ["Bash", "BashOutput", "BashKill"] or set(init.tools) == {
        "Bash",
        "BashOutput",
        "BashKill",
    }
    assert str(host_dir) in text_of(done[0]) and "[exit code 0]" in text_of(done[0])
    assert done[1].is_error and done[1].error_code == "timeout" and done[1].duration_ms < 8000
    record(AREA, "3.2", code=done[1].error_code, duration_ms=done[1].duration_ms)
    out = ends(envelopes, "BashOutput")[0]
    record(AREA, "3.1-background", text=text_of(out), elapsed_s=round(elapsed, 2))
    assert "bg-remote-done" in text_of(out)
    assert result_of(envelopes).subtype == "success"


@needs_server
async def test_a_flood_past_256_mib_ends_with_output_limit() -> None:
    steps: list[Any] = [bash("yes | head -c 314572800; echo after", timeout_ms=120000)]
    model = await plan_model(steps).start()
    try:
        with workspace() as root:
            async with Server() as server:
                agent = await server.open_agent(
                    remote_config(model, root), execution=SubprocessHost()
                )
                start = time.monotonic()
                envelopes = await finish(agent, await agent.run("s1", "go"))
                took = time.monotonic() - start
    finally:
        await model.stop()
    [end] = ends(envelopes, "Bash")
    record(
        AREA,
        "3.3-foreground",
        code=end.error_code,
        text=text_of(end)[-240:],
        text_len=len(text_of(end)),
        took_s=round(took, 2),
    )
    assert end.is_error and end.error_code == "output_limit"
    assert "[output limit reached (256 MiB); the command was killed]" in text_of(end)


@needs_server
async def test_a_background_flood_reads_output_limit_status() -> None:
    def poll(text: str) -> tuple[str, dict[str, object]]:
        return ("BashOutput", {"process_id": PID_RE.search(text).group(0), "wait_ms": 100000})  # type: ignore[union-attr]

    steps: list[Any] = [bash("yes | head -c 314572800", run_in_background=True), poll]
    model = await plan_model(steps).start()
    try:
        with workspace() as root:
            async with Server() as server:
                agent = await server.open_agent(
                    remote_config(model, root), execution=SubprocessHost()
                )
                start = time.monotonic()
                envelopes = await finish(agent, await agent.run("s1", "go"))
                took = time.monotonic() - start
    finally:
        await model.stop()
    [end] = ends(envelopes, "BashOutput")
    record(
        AREA,
        "3.3-background",
        metadata=end.metadata,
        code=end.error_code,
        text=text_of(end)[-200:],
        took_s=round(took, 2),
    )
    # an error result carries no metadata to the event stream (events.py): the code and text are
    # what a client can read; the doc's `status: output_limit` is not observable there.
    assert end.is_error and end.error_code == "output_limit"
    assert "[output limit reached (256 MiB); the command was killed]" in text_of(end)


@needs_server
async def test_the_host_gets_the_principal_of_each_run() -> None:
    counter = {"n": 0}

    def one_command_per_run(seen: Any) -> Any:
        if seen.body["messages"][-1]["role"] == "tool":
            return ("text", "done")
        counter["n"] += 1
        return ("tools", [("Bash", {"command": f"echo run-{counter['n']}"})])

    model = await FakeModel(one_command_per_run).start()
    host = Recording()
    try:
        with workspace() as root:
            tenants = {
                "acme": tenant(
                    "tok-acme-0001",
                    exec_tools=True,
                    permission_modes=["default", "skip_dangerous"],
                    workspace_roots=[str(root)],
                    provider_base_urls=[model.url],
                    execution=[REMOTE],
                )
            }
            async with serving(tenants) as proc:
                async with proc.connect("tok-acme-0001") as server:
                    agent = await server.open_agent(
                        remote_config(model, root, namespace="acme"), execution=host
                    )
                    alice = {"tenant": "acme", "user": "alice", "roles": ["admin"]}
                    bob = {"tenant": "acme", "user": "bob"}
                    await finish(agent, await agent.run("s1", "go", principal=alice))
                    await finish(agent, await agent.run("s2", "go", principal=bob))
                    await finish(agent, await agent.run("s3", "go"))
    finally:
        await model.stop()
    seen = [
        (s.session_id, s.command, dict(s.principal) if s.principal else None) for s in host.starts
    ]
    record(AREA, "3.4", seen=seen)
    assert len(host.starts) == 3
    assert host.starts[0].principal == {"tenant": "acme", "user": "alice", "roles": ["admin"]}
    assert host.starts[1].principal is not None and host.starts[1].principal["user"] == "bob"
    assert host.starts[1].principal.get("roles", []) == []
    assert host.starts[2].principal is None


@needs_server
async def test_connection_end_and_client_death_with_a_background_command() -> None:
    model = await plan_model([bash("sleep 301", run_in_background=True)]).start()
    try:
        with workspace() as root:
            tenants = {
                "acme": tenant(
                    "tok-acme-0001",
                    exec_tools=True,
                    permission_modes=["default", "skip_dangerous"],
                    workspace_roots=[str(root)],
                    provider_base_urls=[model.url],
                    execution=[REMOTE],
                )
            }
            async with serving(tenants) as proc:
                # clean connection close
                host = SubprocessHost()
                server = proc.connect("tok-acme-0001")
                await server.__aenter__()
                agent = await server.open_agent(
                    remote_config(model, root, namespace="acme"), execution=host
                )
                envelopes = await finish(agent, await agent.run("s1", "go"))
                pid = ends(envelopes, "Bash")[0].metadata.get("pid")
                bg_pids = [p for p in descendants(proc.pid)]
                record(AREA, "3.5-clean-before", server_descendants=bg_pids, metadata_pid=pid)
                await server.__aexit__(None, None, None)
                gone = await until_async(lambda: "sleep 301" not in _sleepers(), 5)
                record(AREA, "3.5-clean-close", sleeper_gone_after_s=gone)
                assert gone is not None
                assert descendants(proc.pid) == []

                # client process killed with SIGKILL: no host code can run
                model.requests.clear()
                model.handler = plan_model([bash("sleep 302", run_in_background=True)]).handler
                config = remote_config(model, root, namespace="acme")
                client = drop_client(proc.address, "tok-acme-0001", config, root, "remote")
                try:
                    await wait_line(client, "RUN_ENDED")
                    await asyncio.sleep(0.5)
                    before = _sleepers()
                    client.send_signal(signal.SIGKILL)
                    client.wait()
                    gone = await until_async(lambda: "sleep 302" not in _sleepers(), 8)
                    survivors = _sleepers()
                    record(
                        AREA,
                        "3.5-client-sigkill",
                        sleepers_before=before,
                        sleeper_gone_after_s=gone,
                        survivors=survivors,
                    )
                finally:
                    client.kill()
                    subprocess.run(
                        ["pkill", "-f", "-x", "sleep 302"], check=False
                    )  # my own sleeper
    finally:
        await model.stop()


def _sleepers() -> list[str]:
    done = subprocess.run(["pgrep", "-af", "^sleep 30[12]$"], capture_output=True, text=True)
    return [line.split(" ", 1)[1] for line in done.stdout.splitlines() if " " in line]


@needs_server
async def test_tenant_remote_admission_and_background_refusal(pg_dsn: str) -> None:
    model = await plan_model([bash("echo remote-by-tenant")]).start()
    try:
        with workspace() as root:
            common = {
                "exec_tools": True,
                "permission_modes": ["default", "skip_dangerous"],
                "workspace_roots": [str(root)],
                "provider_base_urls": [model.url],
                "postgres_dsns": [pg_dsn],
                "max_schedules": 4,
                "max_queued_jobs": 4,
                "min_schedule_interval_ms": 1000,
            }
            tenants = {
                "plain": tenant("tok-plain-0001", **common),
                "remote": tenant("tok-remote-0001", **common, execution=[REMOTE]),
            }
            async with serving(tenants) as proc:
                # default list: remote denied
                async with proc.connect("tok-plain-0001") as server:
                    with pytest.raises(NotAllowed) as denied:
                        await server.open_agent(
                            remote_config(model, root, namespace="plain"),
                            execution=SubprocessHost(),
                        )
                    record(
                        AREA, "3.6-denied", kind=denied.value.kind, message=str(denied.value)[:300]
                    )
                    assert denied.value.kind == "execution"
                async with proc.connect("tok-remote-0001") as server:
                    # a policy that differs from the entry
                    with pytest.raises(NotAllowed) as differ:
                        await server.open_agent(
                            remote_config(
                                model,
                                root,
                                namespace="remote",
                                execution={"backend": "remote", "shared_workspace": True},
                            ),
                            execution=SubprocessHost(),
                        )
                    record(AREA, "3.6-differs", kind=differ.value.kind)
                    assert differ.value.kind == "execution"
                    # the exact entry works and Bash runs on the client
                    agent = await server.open_agent(
                        remote_config(model, root, namespace="remote"),
                        execution=SubprocessHost(cwd=root),
                    )
                    envelopes = await finish(agent, await agent.run("s1", "go"))
                    [end] = ends(envelopes, "Bash")
                    record(AREA, "3.6-works", text=text_of(end))
                    assert not end.is_error and "remote-by-tenant" in text_of(end)
                    # no config naming a backend: the first entry (remote) applies
                    model.requests.clear()
                    plain_config = remote_config(model, root, namespace="remote/x")
                    del plain_config["execution"]
                    agent2 = await server.open_agent(plain_config, execution=SubprocessHost())
                    record(AREA, "3.6-default-first-entry", opened=agent2 is not None)

                    # a remote agent with stores in Postgres cannot be scheduled or run by a worker
                    stored = remote_config(model, root, namespace="remote/sched")
                    stored["stores"] = {"type": "postgres", "dsn": pg_dsn}
                    agent3 = await server.open_agent(stored, execution=SubprocessHost())
                    errors: dict[str, Any] = {}
                    for label, call in (
                        ("schedule", agent3.create_schedule("go", interval_ms=60_000, id="r1")),
                        ("enqueue", agent3.enqueue("s9", "go")),
                    ):
                        try:
                            await call
                            errors[label] = "accepted"
                        except RpcError as error:
                            errors[label] = (error.code, str(error)[:200], error.data)
                    record(AREA, "3.7", errors=errors)
                    for label in ("schedule", "enqueue"):
                        assert errors[label] != "accepted", label
                        assert errors[label][0] == -32040
    finally:
        await model.stop()
