"""Live toolsec: the Linux sandbox backend (Landlock, seccomp, rlimits) on a real server.

Expectations are in `toolsec/expectations.md` section 2 (ids 2.1 to 2.6).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ts_support import (
    CANARY,
    ends,
    fake_config,
    finish,
    needs_server,
    plan_model,
    pytestmark,
    record,
    server_env,
    skip_dangerous,
    text_of,
    workspace,
)

from linch_rust import OpenError, Server

__all__ = ["pytestmark"]

AREA = "sandbox"


def bash(command: str, **extra: object) -> tuple[str, dict[str, object]]:
    return ("Bash", {"command": command, **extra})


def py(code: str) -> str:
    return "python3 -c '" + code.replace("'", "'\"'\"'") + "'"


def sock(family: str) -> str:
    return py(
        "import socket,errno,sys\n"
        "try:\n"
        f" socket.socket(socket.{family}, socket.SOCK_STREAM); print('opened')\n"
        "except OSError as e:\n"
        " print('refused', errno.errorcode.get(e.errno, e.errno))"
    )


def syscall(number: int, *args: int) -> str:
    argument = ",".join(str(a) for a in args)
    return py(
        "import ctypes,errno\n"
        "libc=ctypes.CDLL(None,use_errno=True)\n"
        f"r=libc.syscall({number},{argument})\n"
        "print('ret',r,'errno',errno.errorcode.get(ctypes.get_errno(),ctypes.get_errno()))"
    )


async def open_sandbox(model: Any, server: Server, root: Path, **policy: Any) -> Any:
    return await server.open_agent(
        fake_config(
            model,
            workspace=str(root),
            permissions=skip_dangerous(root),
            execution={"backend": "sandbox", **policy},
        )
    )


@needs_server
async def test_sandbox_confines_files_network_syscalls_and_environment() -> None:
    outside = Path(tempfile.mkdtemp(prefix="ts-outside-", dir="/tmp"))
    home_file = Path.home() / ".ts-sbx-probe"
    home_file.write_text("secret-in-home")
    names = [
        "2.1-inside-write",
        "2.1-outside-write",
        "2.1-home-write",
        "2.1-home-read",
        "2.1-server-environ",
        "2.2-inet",
        "2.2-unix",
        "2.2-socketpair",
        "2.4-clone3",
        "2.4-io_uring_setup",
        "2.4-unshare",
        "2.4-mount-syscall",
        "2.4-fork-works",
        "2.5-getcpu",
        "2.5-sched_getaffinity",
        "2.5-getrandom",
        "2.5-membarrier",
        "2.6-env",
    ]
    box: dict[str, Any] = {}
    steps = [
        bash("echo inside > ok.txt && cat ok.txt"),
        bash(f"echo x > {outside}/outside.txt; echo rc=$?"),
        bash(f"echo x > {home_file}.new; echo rc=$?"),
        bash(f"cat {home_file}; echo rc=$?"),
        lambda _: bash(f"cat /proc/{box['pid']}/environ; echo rc=$?"),
        bash(sock("AF_INET")),
        bash(sock("AF_UNIX")),
        bash(
            py("import socket;a,b=socket.socketpair();a.send(b'x');print('socketpair',b.recv(1))")
        ),
        bash(syscall(435, 0, 0)),
        bash(syscall(425, 1, 0)),
        bash("unshare -r true; echo rc=$?"),
        bash(syscall(165, 0, 0, 0, 0, 0)),
        bash("sh -c 'true' && echo forked-ok"),
        bash(syscall(318, 0, 0, 0)),
        bash(py("import os;print('affinity',len(os.sched_getaffinity(0))>0)")),
        bash(py("import os;print('urandom',len(os.urandom(8)))")),
        bash(syscall(324, 0, 0, 0)),
        bash("env"),
    ]
    assert len(names) == len(steps)
    model = await plan_model(steps).start()
    try:
        with workspace() as root:
            async with Server(env=server_env()) as server:
                box["pid"] = server.pid
                agent = await open_sandbox(model, server, root)
                envelopes = await finish(agent, await agent.run("s1", "go"))
                done = ends(envelopes, "Bash")
                outside_exists = (outside / "outside.txt").exists()
                home_new = Path(f"{home_file}.new").exists()
                ok_txt = (root / "ok.txt").read_text() if (root / "ok.txt").exists() else None
    finally:
        await model.stop()
        shutil.rmtree(outside, ignore_errors=True)
        home_file.unlink(missing_ok=True)
        Path(f"{home_file}.new").unlink(missing_ok=True)
    assert len(done) == len(names), [text_of(d)[:80] for d in done]
    out = dict(zip(names, done, strict=True))
    for name, end in out.items():
        record(AREA, name, is_error=end.is_error, code=end.error_code, text=text_of(end)[:700])

    assert ok_txt == "inside\n"
    assert not outside_exists and "Permission denied" in text_of(out["2.1-outside-write"])
    assert not home_new and "Permission denied" in text_of(out["2.1-home-write"])
    assert "secret-in-home" not in text_of(out["2.1-home-read"])
    assert "Permission denied" in text_of(out["2.1-home-read"])
    assert "Permission denied" in text_of(out["2.1-server-environ"])
    assert "refused EACCES" in text_of(out["2.2-inet"])
    assert "refused" in text_of(out["2.2-unix"])
    assert "socketpair" in text_of(out["2.2-socketpair"]) and "refused" not in text_of(
        out["2.2-socketpair"]
    )
    assert "errno ENOSYS" in text_of(out["2.4-clone3"])
    assert "ret -1" in text_of(out["2.4-io_uring_setup"])
    assert "rc=0" not in text_of(out["2.4-unshare"])
    assert "ret -1" in text_of(out["2.4-mount-syscall"])
    assert "forked-ok" in text_of(out["2.4-fork-works"])
    # the denylist wording: syscalls it does not name run
    for name in ("2.5-getcpu", "2.5-sched_getaffinity", "2.5-getrandom"):
        assert not out[name].is_error, name
    assert "ret 0" in text_of(out["2.5-getcpu"])
    env_text = text_of(out["2.6-env"])
    assert CANARY not in env_text and "postgres://" not in env_text
    home_line = [line for line in env_text.splitlines() if line.startswith("HOME=")]
    assert home_line and str(Path.home()) != home_line[0].split("=", 1)[1]
    tmp_line = [line for line in env_text.splitlines() if line.startswith("TMPDIR=")]
    assert tmp_line and tmp_line[0] != "TMPDIR=/tmp"


@needs_server
async def test_sandbox_with_network_opens_inet_only() -> None:
    steps = [
        bash(sock("AF_INET")),
        bash(sock("AF_INET6")),
        bash(
            py(
                "import socket,errno\ntry:\n socket.socket(16,socket.SOCK_RAW,0);print('opened')\nexcept OSError as e:\n print('refused',errno.errorcode.get(e.errno))"
            )
        ),
        bash(
            py(
                "import socket,errno\ntry:\n socket.socket(17,socket.SOCK_RAW,0);print('opened')\nexcept OSError as e:\n print('refused',errno.errorcode.get(e.errno))"
            )
        ),
        bash(sock("AF_UNIX")),
    ]
    model = await plan_model(steps).start()
    try:
        with workspace() as root:
            async with Server(env=server_env()) as server:
                agent = await open_sandbox(model, server, root, network=True)
                done = ends(await finish(agent, await agent.run("s1", "go")), "Bash")
    finally:
        await model.stop()
    texts = [text_of(d).strip() for d in done]
    record(AREA, "2.3", texts=texts)
    assert "opened" in texts[0] and "opened" in texts[1]
    assert "refused" in texts[2] and "refused" in texts[3] and "refused" in texts[4]


@needs_server
async def test_sandbox_limits_and_unprivileged_open() -> None:
    steps = [
        bash("ulimit -v; ulimit -u; ulimit -t; ulimit -f"),
        bash(py("x=bytearray(3*1024*1024*1024);print('allocated')")),
    ]
    model = await plan_model(steps).start()
    try:
        with workspace() as root:
            async with Server(env=server_env()) as server:
                agent = await open_sandbox(model, server, root, memory_bytes=1 << 30)
                done = ends(await finish(agent, await agent.run("s1", "go")), "Bash")
    finally:
        await model.stop()
    record(AREA, "2.limits", texts=[text_of(d)[:400] for d in done])
    assert "allocated" not in text_of(done[1])


@needs_server
async def test_an_invalid_sandbox_policy_does_not_open() -> None:
    model = await plan_model([]).start()
    try:
        with workspace() as root:
            async with Server(env=server_env()) as server:
                try:
                    await open_sandbox(model, server, root, read_paths=["relative/path"])
                except OpenError as error:
                    record(AREA, "2.invalid-policy", error=str(error)[:300])
                else:
                    raise AssertionError("a relative read_paths opened")
    finally:
        await model.stop()
    assert os.getuid() != 0
