"""Live toolsec: the guarantees of the local, sandbox and docker Bash backends on a real server.

No LLM: a programmable fake model issues the commands. Each test names one guarantee and runs it
through a real `linch-server` and a real backend (Landlock plus seccomp plus rlimits, or a real
Docker daemon). Run:

    LINCH_SERVER_BIN=... pytest -m live tests/live/test_ts_backends.py
    LINCH_LIVE_DOCKER=1 LINCH_SERVER_BIN=... sg docker -c "pytest -m live test_ts_backends.py"

The docker cases run only with `LINCH_LIVE_DOCKER=1` and a reachable daemon, and use
`LINCH_LIVE_DOCKER_IMAGE` (default `alpine:3.20`). The containers are capped (memory, cpus, pids),
unprivileged, and named with the prefix `linch-live-` (a `docker_path` wrapper renames the
backend's own `linch-<hex>` names), so a sweep with `docker ps -a --filter name=linch-live-` finds
every one.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from ts_support import (
    CANARY,
    alive,
    descendants,
    ends,
    fake_config,
    finish,
    needs_server,
    plan_model,
    pytestmark,
    record,
    rss_kb,
    server_env,
    serving,
    skip_dangerous,
    tenant,
    text_of,
    until_async,
    workspace,
)

from linch_rust import OpenError, Server, ToolCallStartEvent

__all__ = ["pytestmark"]

AREA = "backends"
IMAGE = os.environ.get("LINCH_LIVE_DOCKER_IMAGE", "alpine:3.20")
PREFIX = "linch-live-"
SANDBOX_ONLY = pytest.mark.parametrize("backend", ["sandbox"])
CONFINED = pytest.mark.parametrize("backend", ["sandbox", "docker"])
ALL = pytest.mark.parametrize("backend", ["local", "sandbox", "docker"])


def bash(command: str, **extra: object) -> tuple[str, dict[str, object]]:
    return ("Bash", {"command": command, **extra})


def py(code: str) -> str:
    return "python3 -c '" + code.replace("'", "'\"'\"'") + "'"


# ---- capability detection -------------------------------------------------------------------


def docker_binary() -> str | None:
    return shutil.which("docker")


def docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    binary = docker_binary()
    assert binary is not None
    return subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)


def docker_skip_reason() -> str | None:
    """Why the docker cases cannot run here, or None when they can."""
    if sys.platform != "linux":
        return "docker cases run on Linux only"
    if os.environ.get("LINCH_LIVE_DOCKER") != "1":
        return "set LINCH_LIVE_DOCKER=1 to run the docker cases"
    if docker_binary() is None:
        return "no docker client on PATH"
    try:
        info = docker("info", "--format", "{{.ServerVersion}}", timeout=30)
    except subprocess.TimeoutExpired:
        return "docker info timed out"
    if info.returncode != 0:
        return "no reachable docker daemon (run under `sg docker -c ...`)"
    if docker("image", "inspect", IMAGE).returncode != 0:
        pulled = docker("pull", "--quiet", IMAGE, timeout=300)
        if pulled.returncode != 0:
            return f"image {IMAGE} is absent and could not be pulled"
    return None


WRAPPER = """#!/bin/sh
# Rename the backend's linch-<hex> container names to linch-live-<hex>, then run the real client.
n=$#
i=0
while [ "$i" -lt "$n" ]; do
  a=$1
  shift
  case "$a" in
    linch-????????????????) a="linch-live-${a#linch-}" ;;
  esac
  set -- "$@" "$a"
  i=$((i + 1))
done
exec "$DOCKER_REAL" "$@"
"""


def make_wrapper(directory: Path) -> str:
    path = directory / "docker-live"
    path.write_text(WRAPPER.replace('"$DOCKER_REAL"', f'"{docker_binary()}"'))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def live_containers() -> list[str]:
    done = docker("ps", "-a", "--filter", f"name={PREFIX}", "--format", "{{.Names}}")
    return done.stdout.split()


def policy(backend: str, wrapper: str | None = None, **extra: Any) -> dict[str, Any]:
    if backend == "local":
        return {"backend": "local"}
    if backend == "sandbox":
        return {"backend": "sandbox", **extra}
    assert wrapper is not None
    base = {
        "backend": "docker",
        "image": IMAGE,
        "docker_path": wrapper,
        "memory_bytes": 256 << 20,
        "cpu_millis": 500,
        "pids_limit": 64,
        "read_only_root": True,
        "tmpfs": ["/tmp:size=16m"],
    }
    return {**base, **extra}


@dataclass
class Box:
    backend: str
    wrapper: str | None

    def execution(self, **extra: Any) -> dict[str, Any]:
        return policy(self.backend, self.wrapper, **extra)


@asynccontextmanager
async def boxed(backend: str) -> AsyncIterator[Box]:
    """A backend ready to use, or a skip with the reason it cannot be."""
    if sys.platform != "linux":
        pytest.skip("the sandbox and docker backends run on Linux only")
    scratch = Path(tempfile.mkdtemp(prefix="tsbk-", dir="/tmp"))
    try:
        if backend == "docker":
            reason = docker_skip_reason()
            if reason:
                pytest.skip(reason)
            yield Box(backend, make_wrapper(scratch))
        else:
            yield Box(backend, None)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@dataclass
class Session:
    server: Server
    agent: Any
    root: Path
    model: Any

    async def run(self, sid: str = "s1") -> list[Any]:
        return await finish(self.agent, await self.agent.run(sid, "go"))


@asynccontextmanager
async def opened(box: Box, steps: list[Any], **extra: Any) -> AsyncIterator[Session]:
    model = await plan_model(steps).start()
    try:
        with workspace() as root:
            async with Server(env=server_env()) as server:
                try:
                    agent = await server.open_agent(
                        fake_config(
                            model,
                            workspace=str(root),
                            permissions=skip_dangerous(root),
                            execution=box.execution(**extra),
                        )
                    )
                except OpenError as error:
                    pytest.skip(f"this host cannot open a {box.backend} agent: {error}")
                yield Session(server, agent, root, model)
    finally:
        await model.stop()


def marker_gone(marker: str) -> bool:
    return subprocess.run(["pgrep", "-f", marker], capture_output=True).returncode != 0


def command_running(marker: str, backend: str) -> bool:
    """Whether the command runs: for docker, in a started container (the client's own command
    line carries the marker earlier, while the daemon is still creating the container)."""
    if marker_gone(marker):
        return False
    if backend != "docker":
        return True
    done = docker("ps", "--filter", f"name={PREFIX}", "--filter", "status=running", "-q")
    return bool(done.stdout.split())


async def no_live_containers(timeout: float = 15) -> bool:
    return await until_async(lambda: not live_containers(), timeout) is not None


# ---- every backend: the command runs ---------------------------------------------------------


@ALL
@needs_server
async def test_a_command_returns_its_output_and_its_exit_status(backend: str) -> None:
    async with boxed(backend) as box:
        steps = [bash("echo out; echo err >&2"), bash("sh -c 'exit 7'"), bash("echo after")]
        async with opened(box, steps) as session:
            done = ends(await session.run(), "Bash")
    ok, failed, after = (text_of(e) for e in done)
    record(AREA, f"{backend}-exit-status", ok=ok, failed=failed)
    assert "out" in ok and "err" in ok and "[exit code 0]" in ok and not done[0].is_error
    assert done[1].is_error and done[1].error_code == "exit_status" and "7" in failed
    assert "after" in after and not done[2].is_error, "the run survives a failing command"


@ALL
@needs_server
async def test_a_command_starts_in_the_workspace_and_its_files_are_the_file_tools_files(
    backend: str,
) -> None:
    async with boxed(backend) as box:
        steps = [bash("pwd; echo made > made.txt; cat in.txt")]
        async with opened(box, steps) as session:
            (session.root / "in.txt").write_text("host-file\n")
            done = ends(await session.run(), "Bash")
            made = (session.root / "made.txt").read_text()
            root = str(session.root)
    assert root in text_of(done[0]) and "host-file" in text_of(done[0])
    assert made == "made\n"


@ALL
@needs_server
async def test_the_servers_secrets_do_not_reach_the_command(backend: str) -> None:
    async with boxed(backend) as box:
        async with opened(box, [bash("env"), bash("cat /proc/self/environ | tr '\\0' '\\n'")]) as s:
            done = ends(await s.run(), "Bash")
    for end in done:
        text = text_of(end)
        assert CANARY not in text and "postgres://" not in text and "sk-" not in text, text[:300]


@CONFINED
@needs_server
async def test_the_servers_own_environ_is_not_readable_from_the_command(backend: str) -> None:
    async with boxed(backend) as box:
        model_steps: list[Any] = []
        async with opened(box, model_steps) as probe:
            pid = probe.server.pid
        steps = [bash(f"cat /proc/{pid}/environ; echo rc=$?")]
        async with opened(box, steps) as session:
            pid = session.server.pid
            steps[0] = bash(f"cat /proc/{pid}/environ; echo rc=$?")
            done = ends(await session.run(), "Bash")
    text = text_of(done[0])
    assert CANARY not in text and "rc=0" not in text, text[:300]


# ---- confinement of files --------------------------------------------------------------------


@CONFINED
@needs_server
async def test_a_write_outside_the_workspace_does_not_reach_the_host(backend: str) -> None:
    outside = Path(tempfile.mkdtemp(prefix="tsbk-out-", dir="/tmp"))
    try:
        async with boxed(backend) as box:
            steps = [
                bash(f"echo x > {outside}/planted.txt; echo rc=$?"),
                bash("echo x > /etc/linch-live-probe; echo rc=$?"),
            ]
            async with opened(box, steps) as session:
                done = ends(await session.run(), "Bash")
        planted = (outside / "planted.txt").exists()
    finally:
        shutil.rmtree(outside, ignore_errors=True)
    record(AREA, f"{backend}-write-outside", texts=[text_of(e)[:200] for e in done])
    assert not planted and not Path("/etc/linch-live-probe").exists()
    assert all("rc=0" not in text_of(e) for e in done), [text_of(e) for e in done]
    expected = "Permission denied" if backend == "sandbox" else "Read-only file system"
    assert expected in text_of(done[1]) or "No such file" in text_of(done[0])


@CONFINED
@needs_server
async def test_a_host_file_beside_the_workspace_cannot_be_read(backend: str) -> None:
    outside = Path(tempfile.mkdtemp(prefix="tsbk-out-", dir="/tmp"))
    (outside / "secret.txt").write_text("host-secret-" + CANARY)
    home_secret = Path.home() / ".linch-live-probe"
    home_secret.write_text("home-secret-" + CANARY)
    try:
        async with boxed(backend) as box:
            steps = [
                bash(f"cat {outside}/secret.txt; echo rc=$?"),
                bash(f"cat {home_secret}; echo rc=$?"),
                bash("cat ../secret-not-there 2>&1; echo rc=$?"),
            ]
            async with opened(box, steps) as session:
                done = ends(await session.run(), "Bash")
    finally:
        shutil.rmtree(outside, ignore_errors=True)
        home_secret.unlink(missing_ok=True)
    for end in done[:2]:
        text = text_of(end)
        assert CANARY not in text and "rc=0" not in text, text[:300]


# ---- seccomp, capabilities, namespaces -------------------------------------------------------


def syscall(number: int, *args: int) -> str:
    argument = ",".join(str(a) for a in args)
    return py(
        "import ctypes,errno\n"
        "libc=ctypes.CDLL(None,use_errno=True)\n"
        f"r=libc.syscall({number},{argument})\n"
        "print('ret',r,'errno',errno.errorcode.get(ctypes.get_errno(),ctypes.get_errno()))"
    )


# Linux x86-64 numbers of the calls the sandbox's seccomp filter refuses with EPERM.
DENIED = {"mount": 165, "chroot": 161, "pivot_root": 155, "keyctl": 250, "userfaultfd": 323}


@SANDBOX_ONLY
@needs_server
async def test_seccomp_refuses_a_forbidden_syscall_with_an_error_result_not_a_crash(
    backend: str,
) -> None:
    if os.uname().machine != "x86_64":
        pytest.skip("the probe uses x86-64 syscall numbers")
    names = list(DENIED)
    steps = [
        bash(syscall(165, 0, 0, 0, 0, 0)),
        bash(syscall(161, 0)),
        bash(syscall(155, 0, 0)),
        bash(syscall(250, 0, -4, 0)),
        bash(syscall(323, 0)),
        bash("unshare -r true; echo rc=$?"),
        bash(f"echo survived-{os.getpid()}"),
    ]
    async with boxed("local") as local, boxed(backend) as box:
        async with opened(local, steps) as control:
            controls = [text_of(e) for e in ends(await control.run(), "Bash")]
        async with opened(box, steps) as session:
            done = ends(await session.run(), "Bash")
            texts = [text_of(e) for e in done]
    record(AREA, "sandbox-seccomp", texts=texts, local_control=controls)
    assert len(done) == len(steps), "a refused syscall must not end the run"
    for name, text in zip(names, texts, strict=False):
        assert "ret -1" in text and "EPERM" in text, f"{name}: {text}"
    assert "rc=0" not in texts[5]
    assert f"survived-{os.getpid()}" in texts[6]
    # keyctl succeeds for the unconfined user, so the refusal is the filter's own
    assert texts[3] != controls[3]


@SANDBOX_ONLY
@needs_server
async def test_a_command_cannot_ptrace_a_process_outside_the_sandbox(backend: str) -> None:
    async with boxed(backend) as box:
        async with opened(box, []) as first:
            pid = first.server.pid
        steps = [bash("true")]
        async with opened(box, steps) as session:
            pid = session.server.pid
            code = (
                "import ctypes,errno\n"
                "libc=ctypes.CDLL(None,use_errno=True)\n"
                f"r=libc.ptrace(16,{pid},0,0)\n"
                "print('ret',r,'errno',errno.errorcode.get(ctypes.get_errno(),ctypes.get_errno()))"
            )
            steps[0] = bash(py(code))
            done = ends(await session.run(), "Bash")
    text = text_of(done[0])
    record(AREA, "sandbox-ptrace", text=text)
    assert "ret -1" in text and ("EPERM" in text or "EACCES" in text), text


@pytest.mark.parametrize("backend", ["docker"])
@needs_server
async def test_a_container_has_no_capabilities_and_cannot_mount(backend: str) -> None:
    async with boxed(backend) as box:
        steps = [
            bash("grep -E '^(CapEff|CapBnd|NoNewPrivs)' /proc/self/status"),
            bash("mount -t tmpfs none /tmp 2>&1; echo rc=$?"),
            bash("chown 0:0 /tmp 2>&1; echo rc=$?"),
        ]
        async with opened(box, steps) as session:
            done = ends(await session.run(), "Bash")
    caps, mount, chown = (text_of(e) for e in done)
    assert "CapEff:\t0000000000000000" in caps and "CapBnd:\t0000000000000000" in caps, caps
    assert "NoNewPrivs:\t1" in caps, caps
    assert "rc=0" not in mount and "rc=0" not in chown, (mount, chown)


# ---- resource limits -------------------------------------------------------------------------


@SANDBOX_ONLY
@needs_server
async def test_sandbox_memory_cpu_and_file_size_limits_fail_inside_the_command(
    backend: str,
) -> None:
    steps = [
        bash(py("x=bytearray(3*1024*1024*1024);print('allocated')")),
        bash("while :; do :; done", timeout_ms=30000),
        bash("head -c 20000000 /dev/zero > big.bin; echo rc=$?; ls -l big.bin | wc -c"),
        bash("echo still-alive"),
    ]
    async with boxed(backend) as box:
        async with opened(
            box,
            steps,
            memory_bytes=1 << 30,
            cpu_seconds=2,
            file_size_bytes=1 << 20,
        ) as session:
            started = time.monotonic()
            done = ends(await session.run(), "Bash")
            took = time.monotonic() - started
    texts = [text_of(e) for e in done]
    record(AREA, "sandbox-rlimits", texts=[t[:200] for t in texts], took_s=round(took, 1))
    assert "allocated" not in texts[0] and done[0].is_error
    assert done[1].is_error and took < 25, "the busy loop ends at its CPU limit, not its timeout"
    assert "rc=0" not in texts[2]
    assert "still-alive" in texts[3], "the run survives every limit"


@needs_server
@pytest.mark.parametrize("backend", ["docker"])
async def test_a_container_memory_hog_is_killed_at_the_memory_limit(backend: str) -> None:
    async with boxed(backend) as box:
        steps = [bash("tail /dev/zero", timeout_ms=60000), bash("echo still-alive")]
        async with opened(box, steps) as session:
            done = ends(await session.run(), "Bash")
    texts = [text_of(e) for e in done]
    record(AREA, "docker-memory", texts=[t[:200] for t in texts])
    assert done[0].is_error and "137" in texts[0], texts[0]
    assert "still-alive" in texts[1]
    assert await no_live_containers()


@CONFINED
@needs_server
async def test_a_fork_bomb_stays_bounded_and_the_run_survives(backend: str) -> None:
    marker = "31338"
    limit = 48
    count = f"pgrep -f 'sleep {marker}' | wc -l"
    steps = [
        # a bounded storm: 200 attempts, counted
        bash(
            f"i=0; while [ $i -lt 200 ]; do sleep {marker} 2>/dev/null & i=$((i+1)); done; "
            f"sleep 1; {count}"
        ),
        # a real bomb, cut by the timeout; the group is then killed
        bash("b() { b | b & }; b; sleep 20", timeout_ms=4000),
        bash("echo survived"),
    ]
    extra = {"max_processes": limit} if backend == "sandbox" else {"pids_limit": limit}
    async with boxed(backend) as box:
        async with opened(box, steps, **extra) as session:
            done = ends(await session.run(), "Bash")
            live = len(descendants(session.server.pid))
    texts = [text_of(e) for e in done]
    record(AREA, f"{backend}-forkbomb", texts=[t[:200] for t in texts], left=live)
    counted = [line for line in texts[0].splitlines() if line.strip().isdigit()]
    # on a busy host other work of the same user can use the sandbox's headroom first, so 0 is fine
    storm = int(counted[-1]) if counted else 0
    assert storm <= limit + 8, f"{storm} processes ran against a limit of {limit}"
    assert done[1].is_error, "a bomb that fills the process limit ends as an error result"
    assert "survived" in texts[2]
    assert await until_async(lambda: marker_gone(marker), 10) is not None
    assert await until_async(lambda: descendants(session.server.pid) == [], 10) is not None
    assert await no_live_containers() if backend == "docker" else True


# ---- network ---------------------------------------------------------------------------------


@CONFINED
@needs_server
async def test_the_network_is_off_by_default(backend: str) -> None:
    probe = (
        py(
            "import socket,errno\n"
            "try:\n s=socket.create_connection(('127.0.0.1',9),2);print('connected')\n"
            "except OSError as e:\n print('refused',errno.errorcode.get(e.errno,e.errno))"
        )
        if backend == "sandbox"
        else "cat /proc/net/dev; wget -q -T 3 -O- http://1.1.1.1 2>&1; echo rc=$?"
    )
    async with boxed(backend) as box:
        async with opened(box, [bash(probe)]) as session:
            done = ends(await session.run(), "Bash")
    text = text_of(done[0])
    record(AREA, f"{backend}-network", text=text[:400])
    if backend == "sandbox":
        assert "refused" in text and "connected" not in text, text
    else:
        interfaces = [line.split(":")[0].strip() for line in text.splitlines() if ":" in line]
        assert "eth0" not in interfaces, text
        assert "rc=0" not in text, text


# ---- timeout, output cap, cancel, kill -9 ----------------------------------------------------


@ALL
@needs_server
async def test_a_timeout_kills_the_whole_process_tree_and_leaves_no_orphan(backend: str) -> None:
    marker = "31339"
    steps = [
        bash(f"sh -c 'sleep {marker} & sleep {marker}; wait' & wait", timeout_ms=3000),
        bash("echo survived"),
    ]
    async with boxed(backend) as box:
        async with opened(box, steps) as session:
            started = time.monotonic()
            done = ends(await session.run(), "Bash")
            took = time.monotonic() - started
            gone = await until_async(lambda: marker_gone(marker), 10)
            tree = await until_async(lambda: descendants(session.server.pid) == [], 10)
    record(AREA, f"{backend}-timeout", took_s=round(took, 1), gone_s=gone, tree_s=tree)
    assert done[0].error_code == "timeout" and "survived" in text_of(done[1])
    assert gone is not None, f"orphan `sleep {marker}` after the timeout"
    assert tree is not None
    assert backend != "docker" or await no_live_containers()


@ALL
@needs_server
async def test_a_100_mb_stream_is_capped_and_the_server_stays_small(backend: str) -> None:
    steps = [bash("yes | head -c 104857600", timeout_ms=120000)]
    async with boxed(backend) as box:
        async with opened(box, steps) as session:
            before = rss_kb(session.server.pid)
            peak = {"kb": before}

            async def watch() -> None:
                while True:
                    peak["kb"] = max(peak["kb"], rss_kb(session.server.pid))
                    await asyncio.sleep(0.05)

            watcher = asyncio.create_task(watch())
            try:
                done = ends(await session.run(), "Bash")
            finally:
                watcher.cancel()
    text = text_of(done[0])
    record(
        AREA, f"{backend}-output-cap", text_len=len(text), rss_before=before, rss_peak=peak["kb"]
    )
    assert len(text) < 31500 and "[exit code 0]" in text
    assert peak["kb"] - before < 200 * 1024, "the server's memory must not follow the stream"


async def start_and_wait(session: Session, marker: str, backend: str) -> Any:
    """Start the run and return it once the Bash call has begun."""
    run = await session.agent.run("s1", "go")
    async for envelope in run:
        if isinstance(envelope.event, ToolCallStartEvent):
            break
    await until_async(lambda: command_running(marker, backend), 20)
    return run


@ALL
@needs_server
async def test_cancelling_a_run_leaves_no_orphan_process_and_no_container(backend: str) -> None:
    marker = "31340"
    steps = [bash(f"sleep {marker}", timeout_ms=120000)]
    async with boxed(backend) as box:
        async with opened(box, steps) as session:
            run = await start_and_wait(session, marker, backend)
            assert not marker_gone(marker), "the command never started"
            during = live_containers() if backend == "docker" else []
            await run.cancel()
            async for _ in run:
                pass
            gone = await until_async(lambda: marker_gone(marker), 10)
            tree = await until_async(lambda: descendants(session.server.pid) == [], 10)
    record(AREA, f"{backend}-cancel", gone_s=gone, containers_during=during)
    assert gone is not None and tree is not None
    assert backend != "docker" or (during and await no_live_containers())


def tenants_for(root: Path, model: Any) -> dict[str, Any]:
    return {
        "acme": tenant(
            "tok-acme-0001",
            exec_tools=True,
            permission_modes=["default", "skip_dangerous"],
            workspace_roots=[str(root)],
            provider_base_urls=[model.url],
        )
    }


@ALL
@needs_server
async def test_kill_9_of_the_server_leaves_no_orphan_process_and_no_container(
    backend: str,
) -> None:
    marker = "31341"
    async with boxed(backend) as box:
        # Documented: a foreground local or sandbox command has no lifeline and may outlive a
        # killed server (tools.md); a background one and every docker command do.
        extra: dict[str, object] = {} if backend == "docker" else {"run_in_background": True}
        model = await plan_model([bash(f"sleep {marker}", timeout_ms=120000, **extra)]).start()
        try:
            with workspace() as root:
                tenants = tenants_for(root, model)
                tenants["acme"]["allow"]["execution"] = [box.execution()]
                async with serving(tenants, env=server_env()) as proc:
                    client = proc.connect("tok-acme-0001")
                    async with client:
                        agent = await client.open_agent(
                            fake_config(
                                model,
                                namespace="acme",
                                workspace=str(root),
                                permissions=skip_dangerous(root),
                                execution=box.execution(),
                            )
                        )
                        run = await agent.run("s1", "go")
                        async for envelope in run:
                            if isinstance(envelope.event, ToolCallStartEvent):
                                break
                        await until_async(lambda: command_running(marker, backend), 20)
                        assert not marker_gone(marker), "the command never started"
                        pids = descendants(proc.pid)
                        during = live_containers() if backend == "docker" else []
                        proc.kill()
                        gone = await until_async(lambda: marker_gone(marker), 12)
                        dead = await until_async(lambda: not any(alive(p) for p in pids), 12)
        finally:
            await model.stop()
            subprocess.run(["pkill", "-9", "-f", f"sleep {marker}"], check=False)
        left = live_containers() if backend == "docker" else []
        containers_gone = await no_live_containers(15) if backend == "docker" else True
    record(
        AREA,
        f"{backend}-kill9",
        pids=pids,
        marker_gone_s=gone,
        tree_dead_s=dead,
        containers_during=during,
        containers_left=left,
    )
    assert gone is not None, f"`sleep {marker}` outlived a kill -9 of the server"
    assert dead is not None
    assert containers_gone, f"containers left: {left}"
    if backend == "docker":
        assert during, "no container was seen while the command ran"


# ---- docker: the argument construction, read back from the daemon ----------------------------


@pytest.mark.parametrize("backend", ["docker"])
@needs_server
async def test_the_container_is_created_with_the_documented_limits(backend: str) -> None:
    marker = "31342"
    async with boxed(backend) as box:
        steps = [bash(f"sleep {marker}", timeout_ms=60000)]
        async with opened(box, steps) as session:
            run = await start_and_wait(session, marker, backend)
            names = live_containers()
            assert names, "no container named linch-live-* while the command ran"
            inspected = docker("inspect", names[0])
            await run.cancel()
            async for _ in run:
                pass
            root = str(session.root)
            assert await no_live_containers()
    [info] = json.loads(inspected.stdout)
    host = info["HostConfig"]
    config = info["Config"]
    record(
        AREA, "docker-inspect", host={k: host.get(k) for k in ("Memory", "NanoCpus", "PidsLimit")}
    )
    assert config["User"] == f"{os.getuid()}:{os.getgid()}"
    assert host["CapDrop"] == ["ALL"] and not host["CapAdd"]
    assert "no-new-privileges" in "".join(host["SecurityOpt"])
    assert host["Memory"] == 256 << 20 and host["NanoCpus"] == 500_000_000
    assert host["PidsLimit"] == 64
    assert host["ReadonlyRootfs"] is True and host["Privileged"] is False
    assert host["NetworkMode"] == "none"
    assert host["AutoRemove"] is True
    mounts = [(m["Type"], m["Source"], m["Destination"]) for m in info["Mounts"]]
    assert mounts == [("bind", root, root)], "only the workspace is mounted from the host"
    assert any(label.startswith("linch.boot") for label in config["Labels"])
    assert config["Image"] == IMAGE
    assert info["Name"].lstrip("/").startswith(PREFIX)


def test_the_live_containers_are_swept() -> None:
    """Last in the file: nothing with this suite's prefix is left behind."""
    reason = docker_skip_reason()
    if reason:
        pytest.skip(reason)
    left = live_containers()
    assert left == [], f"leftover containers: {left}"
