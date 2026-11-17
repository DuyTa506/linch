"""Live toolsec: the Bash tool end to end on a real server (no LLM: a programmable fake model).

Expectations are in `toolsec/expectations.md` section 1 (ids 1.1 to 1.11).
"""

from __future__ import annotations

import asyncio
import json
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from ts_support import (
    CANARY,
    HERE,
    alive,
    descendants,
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
    skip_dangerous,
    tenant,
    text_of,
    until_async,
    workspace,
)

from linch_rust import Server

__all__ = ["pytestmark"]

AREA = "bash"
PID_RE = re.compile(r"bash_[0-9a-f]{12}")


def bash(command: str, **extra: object) -> tuple[str, dict[str, object]]:
    return ("Bash", {"command": command, **extra})


def process_id(text: str) -> str:
    match = PID_RE.search(text)
    assert match, text
    return match.group(0)


@needs_server
async def test_foreground_timeout_truncation_and_env() -> None:
    steps = [
        bash("echo hi; pwd"),  # 1.1
        bash("sleep 60", timeout_ms=1500),  # 1.2
        bash(
            "python3 -c \"import sys; sys.stdout.write('H'*4000 + 'M'*20000 + 'T'*26000)\""
        ),  # 1.3
        bash("env"),  # 1.11
        bash("cat /proc/self/environ | tr '\\0' '\\n'"),
        bash("cat /proc/$PPID/environ 2>&1 | tr '\\0' '\\n'; echo ppid=$PPID"),
        bash("sh -c 'exit 7'"),
    ]
    model = await plan_model(steps).start()
    try:
        with workspace() as root:
            async with Server(env=server_env()) as server:
                agent = await server.open_agent(
                    fake_config(model, workspace=str(root), permissions=skip_dangerous(root))
                )
                run = await agent.run("s1", "go")
                envelopes = await finish(agent, run)
    finally:
        await model.stop()
    done = ends(envelopes, "Bash")
    assert len(done) == len(steps)
    hello, slept, big, env_out, self_env, ppid_env, failing = done

    assert not hello.is_error and str(root) in text_of(hello) and "[exit code 0]" in text_of(hello)
    record(AREA, "1.1", text=text_of(hello))

    assert slept.is_error and slept.error_code == "timeout" and slept.duration_ms < 6000
    record(AREA, "1.2", code=slept.error_code, duration_ms=slept.duration_ms)

    text = text_of(big)
    heads, mids, tails = text.count("H"), text.count("M"), text.count("T")
    record(AREA, "1.3", total_len=len(text), H=heads, M=mids, T=tails, sample=text[3990:4200])
    assert heads == 4000 and tails == 26000 and mids < 20000 and len(text) < 31500

    for name, end in (("env", env_out), ("self_environ", self_env), ("ppid_environ", ppid_env)):
        body = text_of(end)
        record(AREA, f"1.11-{name}", text=body[:1500])
        assert CANARY not in body and "TS_CANARY" not in body, name
        assert "LINCH_TEST_PG_DSN" not in body and "postgres://" not in body, name
        assert "DEEPSEEK_API_KEY" not in body, name
    names = {line.split("=", 1)[0] for line in text_of(env_out).splitlines() if "=" in line}
    allowed = {"PATH", "HOME", "TERM", "LANG", "LC_ALL", "LC_CTYPE", "USER", "SHELL", "TMPDIR"}
    extra = names - allowed - {"PWD", "SHLVL", "_", "OLDPWD"}
    record(AREA, "1.11-names", names=sorted(names), unexpected=sorted(extra))
    assert not extra, extra
    assert names >= allowed, f"allowlisted variables missing: {sorted(allowed - names)}"

    assert failing.is_error and failing.error_code == "exit_status" and "7" in text_of(failing)
    assert result_of(envelopes).subtype == "success"


@needs_server
async def test_background_output_kill_cap_and_close_session() -> None:
    def start_bg(text: str) -> tuple[str, dict[str, object]]:
        return ("BashOutput", {"process_id": process_id(text), "wait_ms": 8000})

    def kill_it(text: str) -> tuple[str, dict[str, object]]:
        return ("BashKill", {"process_id": process_id(text)})

    steps: list[object] = [
        bash("sh -c 'sleep 1; echo bg-finished-77'", run_in_background=True),  # 0
        start_bg,  # 1: wait for it
        bash("sleep 300", run_in_background=True),  # 2
        kill_it,  # 3
        bash("sleep 301", run_in_background=True),  # four running ...
        bash("sleep 302", run_in_background=True),
        bash("sleep 303", run_in_background=True),
        bash("sleep 304", run_in_background=True),
        bash("sleep 305", run_in_background=True),  # ... the fifth must fail
    ]
    model = await plan_model(steps).start()
    try:
        with workspace() as root:
            async with Server(env=server_env()) as server:
                agent = await server.open_agent(
                    fake_config(model, workspace=str(root), permissions=skip_dangerous(root))
                )
                run = await agent.run("s1", "go")
                envelopes = await finish(agent, run)
                bg = ends(envelopes, "Bash")
                outs = ends(envelopes, "BashOutput")
                kills = ends(envelopes, "BashKill")
                first = bg[0]
                assert re.fullmatch(r"bash_[0-9a-f]{12}", first.metadata["process_id"])
                assert "BashOutput" in text_of(first) and "pid" in first.metadata
                record(AREA, "1.4-start", text=text_of(first), metadata=first.metadata)
                assert "bg-finished-77" in text_of(outs[0]) and "[exit code 0]" in text_of(outs[0])
                assert not alive(first.metadata["pid"])
                record(AREA, "1.4-output", text=text_of(outs[0]))

                sleeper = bg[1]
                assert not kills[0].is_error
                group_gone = await until_async(lambda: not alive(sleeper.metadata["pid"]), 3)
                record(AREA, "1.5", killed_in_s=group_gone, text=text_of(kills[0]))
                assert group_gone is not None
                sleepers = [b for b in bg[2:] if not b.is_error]
                refused = [b for b in bg[2:] if b.is_error]
                record(
                    AREA,
                    "1.6",
                    started=len(sleepers),
                    refused=[(b.error_code, text_of(b)[:200]) for b in refused],
                )
                assert refused and refused[-1].error_code == "too_many_processes"
                pids = [b.metadata["pid"] for b in sleepers]
                assert all(alive(p) for p in pids)
                await agent.close_session("s1")
                gone = await until_async(lambda: not any(alive(p) for p in pids), 5)
                record(AREA, "1.7", gone_after_close_session_s=gone, pids=pids)
                assert gone is not None
                assert descendants(server.pid) == []
    finally:
        await model.stop()


@needs_server
async def test_local_flood_is_truncated_not_output_limit() -> None:
    steps = [bash("yes | head -c 314572800", timeout_ms=120000)]
    model = await plan_model(steps).start()
    try:
        with workspace() as root:
            async with Server(env=server_env()) as server:
                agent = await server.open_agent(
                    fake_config(model, workspace=str(root), permissions=skip_dangerous(root))
                )
                start = time.monotonic()
                run = await agent.run("s1", "go")
                envelopes = await finish(agent, run)
                took = time.monotonic() - start
    finally:
        await model.stop()
    [end] = ends(envelopes, "Bash")
    text = text_of(end)
    record(
        AREA,
        "1.10",
        code=end.error_code,
        is_error=end.is_error,
        text_len=len(text),
        took_s=round(took, 2),
        tail=text[-160:],
    )
    assert end.error_code != "output_limit"
    assert len(text) < 31500


def drop_client(
    address: str, token: str, config: dict, tmp: Path, *flags: str
) -> subprocess.Popen[str]:
    path = tmp / f"cfg-{time.monotonic_ns()}.json"
    path.write_text(json.dumps(config))
    return subprocess.Popen(
        [sys.executable, str(HERE / "ts_dropclient.py"), address, token, str(path), *flags],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


async def wait_line(client: subprocess.Popen[str], needle: str, timeout: float = 30) -> None:
    assert client.stdout is not None
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(timeout):
        while True:
            line = await loop.run_in_executor(None, client.stdout.readline)
            assert line, f"client ended before {needle}"
            if needle in line:
                return


@needs_server
async def test_a_dropped_client_connection_reaps_background_and_foreground_commands() -> None:
    for mode, command in (("background", "sleep 300"), ("foreground", "sleep 300")):
        extra: dict[str, object] = {"run_in_background": True} if mode == "background" else {}
        model = await plan_model([bash(command, **extra)]).start()
        try:
            with workspace() as root:
                tenants = {
                    "acme": tenant(
                        "tok-acme-0001",
                        exec_tools=True,
                        permission_modes=["default", "skip_dangerous"],
                        workspace_roots=[str(root)],
                        provider_base_urls=[model.url],
                    )
                }
                async with serving(tenants) as proc:
                    config = fake_config(
                        model,
                        namespace="acme",
                        workspace=str(root),
                        permissions=skip_dangerous(root),
                    )
                    client = drop_client(proc.address, "tok-acme-0001", config, root)
                    try:
                        await wait_line(
                            client, "EVENT tool_call_start" if mode == "foreground" else "RUN_ENDED"
                        )
                        await asyncio.sleep(0.5)
                        before = descendants(proc.pid)
                        assert before, f"{mode}: no command under the server"
                        client.send_signal(signal.SIGKILL)
                        client.wait()
                        gone = await until_async(lambda: descendants(proc.pid) == [], 8)
                        left = descendants(proc.pid)
                        record(
                            AREA,
                            f"1.8-{mode}",
                            before=before,
                            reaped_after_s=gone,
                            left=left,
                        )
                        # background: the lifeline plus connection end reap it; foreground: the
                        # run is dropped with its call. Both must leave nothing under the server.
                        assert gone is not None, f"{mode}: orphans {left}"
                    finally:
                        if client.poll() is None:
                            client.kill()
        finally:
            await model.stop()


@needs_server
async def test_server_sigkill_background_lifeline_and_foreground_survival() -> None:
    results = {}
    for mode in ("background", "foreground"):
        extra: dict[str, object] = {"run_in_background": True} if mode == "background" else {}
        model = await plan_model([bash("sleep 301", **extra)]).start()
        try:
            with workspace() as root:
                tenants = {
                    "acme": tenant(
                        "tok-acme-0001",
                        exec_tools=True,
                        permission_modes=["default", "skip_dangerous"],
                        workspace_roots=[str(root)],
                        provider_base_urls=[model.url],
                    )
                }
                async with serving(tenants) as proc:
                    config = fake_config(
                        model,
                        namespace="acme",
                        workspace=str(root),
                        permissions=skip_dangerous(root),
                    )
                    client = drop_client(proc.address, "tok-acme-0001", config, root)
                    try:
                        await wait_line(
                            client, "EVENT tool_call_start" if mode == "foreground" else "RUN_ENDED"
                        )
                        await asyncio.sleep(0.5)
                        pids = descendants(proc.pid)
                        assert pids
                        proc.kill()
                        gone = await until_async(lambda: not any(alive(p) for p in pids), 6)
                        results[mode] = {"pids": pids, "dead_after_s": gone}
                        for p in pids:
                            if alive(p):
                                subprocess.run(["kill", "-9", "--", f"-{p}"], check=False)
                                subprocess.run(["kill", "-9", str(p)], check=False)
                    finally:
                        client.kill()
        finally:
            await model.stop()
    record(AREA, "1.9", **{k: v for k, v in results.items()})
    assert results["background"]["dead_after_s"] is not None
