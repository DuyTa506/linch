"""Live: `execution: sandbox` (Landlock, seccomp, rlimits) against `execution: local`.

One probe script, run by the model through `Bash`, tries each forbidden act and
prints `NAME=<what happened>`; the assertions read those lines and the files
on disk. Kernel support is checked first: an agent that cannot confine does
not open (`OpenError`), and the test then skips with the server's reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
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

from linch_rust import OpenError, Server

pytestmark = pytest.mark.live

RUNNER = (
    "You are a command runner. When told to run a command, call the Bash tool with exactly "
    "that command, and do not run anything else. Then reply DONE."
)

PROBE = """\
echo "READ_WS=$(cat data.txt 2>&1)"
echo "READ_SECRET=$(cat {secret} 2>&1)"
echo "READ_SSH=$(ls -A {ssh} 2>&1 | head -2 | tr '\\n' ' ')"
echo "READ_SHADOW=$(cat /etc/shadow 2>&1 | head -1)"
echo "READ_SERVER_ENV=$( (tr '\\0' '\\n' < /proc/{server_pid}/environ) 2>&1 | head -3 | tr '\\n' ' ')"
echo "ENV_CANARY=$(env | grep -c LIVECORE_CANARY)"
echo "WRITE_OUTSIDE=$( (echo x > {outside}) 2>&1 && echo wrote)"
echo "WRITE_WS=$( (echo y > inside.txt) 2>&1 && echo wrote)"
echo "UNSHARE_USER=$(unshare -U true 2>&1 && echo ok)"
echo "UNSHARE_MOUNT=$(unshare -m true 2>&1 && echo ok)"
echo "MOUNT_BIND=$(mount --bind {ws} {ws} 2>&1 && echo ok)"
echo "NET=$(python3 -c "import socket; socket.create_connection(('127.0.0.1', {port}), 2); print('connected')" 2>&1 | tail -1)"
echo PROBE-END
"""  # noqa: E501


def parse(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.isupper():
            found[name] = value
    return found


async def landlock_or_skip(server: Server, live: Live, workspace: Path, execution: Any) -> Any:
    try:
        return await server.open_agent(
            make_config(
                live,
                live.spec(system_prompt=RUNNER, max_turns=6),
                workspace=str(workspace),
                execution=execution,
                permissions={"mode": "skip_dangerous", "project_root": str(workspace)},
            )
        )
    except OpenError as error:
        pytest.skip(f"this host cannot open a sandbox agent: {error}")


async def probe(live: Live, tmp_path: Path, execution: dict[str, Any]) -> dict[str, str]:
    # A retried scenario gets its own directory: `tmp_path` is shared by both attempts.
    workspace = (tmp_path / f"ws-{uuid.uuid4().hex[:6]}").resolve()
    workspace.mkdir()
    (workspace / "data.txt").write_text("workspace-data-ok\n")
    # Same user, mode 644, outside the workspace: only Landlock can close it.
    secret = tmp_path / f"outside-secret-{uuid.uuid4().hex[:6]}.txt"
    secret.write_text(f"secret-{CANARY}\n")
    outside = Path(f"/tmp/livecore-outside-{uuid.uuid4().hex}")
    ssh = Path.home() / ".ssh"

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    listener = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        async with live_server() as server:
            agent = await landlock_or_skip(server, live, workspace, execution)
            (workspace / "probe.sh").write_text(
                PROBE.format(
                    secret=secret,
                    ssh=ssh,
                    outside=outside,
                    ws=workspace,
                    port=port,
                    server_pid=server.pid,
                )
            )
            run = await agent.run("s1", "Run this command: sh probe.sh")
            trace = await drive(agent, run, timeout=240)
        outside_exists = outside.exists()
    finally:
        listener.close()
        with contextlib.suppress(FileNotFoundError):
            outside.unlink()
    bash = trace.ends("Bash")
    assert bash, "the model never called Bash"
    text = text_of(bash[0])
    assert "PROBE-END" in text, text
    seen = parse(text)
    seen["_outside_exists"] = str(outside_exists)
    seen["_inside_exists"] = str((workspace / "inside.txt").exists())
    return seen


def denied(value: str) -> bool:
    return "denied" in value.lower() or "not permitted" in value.lower() or "operation" in value


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_sandbox_reads_its_workspace_and_refuses_everything_else(
    live: Live, tmp_path: Path
) -> None:
    seen = await probe(live, tmp_path, {"backend": "sandbox"})
    assert seen["READ_WS"] == "workspace-data-ok"
    assert seen["WRITE_WS"] == "wrote" and seen["_inside_exists"] == "True"
    # Closed by Landlock although the file permissions would allow the user.
    assert CANARY not in seen["READ_SECRET"] and denied(seen["READ_SECRET"])
    assert denied(seen["READ_SSH"]), seen["READ_SSH"]
    assert "root:" not in seen["READ_SHADOW"]
    assert denied(seen["READ_SERVER_ENV"]) and CANARY not in seen["READ_SERVER_ENV"]
    assert seen["ENV_CANARY"] == "0"
    assert denied(seen["WRITE_OUTSIDE"]) and seen["_outside_exists"] == "False"
    assert "ok" not in seen["UNSHARE_USER"] and "ok" not in seen["UNSHARE_MOUNT"]
    assert "ok" not in seen["MOUNT_BIND"]
    # Not discriminating on this host: local refuses mount and `unshare -m` too (no
    # privilege, and AppArmor blocks `unshare -Ur`), so the seccomp mount filter is not
    # shown independently. `unshare -U` (ok locally, refused here) shows the unshare filter.
    assert "connected" not in seen["NET"] and denied(seen["NET"])


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_sandbox_network_is_available_only_when_asked(live: Live, tmp_path: Path) -> None:
    seen = await probe(live, tmp_path, {"backend": "sandbox", "network": True})
    assert seen["NET"] == "connected"
    assert seen["READ_WS"] == "workspace-data-ok" and denied(seen["READ_SECRET"])
    assert denied(seen["WRITE_OUTSIDE"])


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_local_execution_for_comparison_is_not_confined(live: Live, tmp_path: Path) -> None:
    seen = await probe(live, tmp_path, {"backend": "local"})
    assert seen["READ_WS"] == "workspace-data-ok"
    assert CANARY in seen["READ_SECRET"]
    assert seen["WRITE_OUTSIDE"] == "wrote" and seen["_outside_exists"] == "True"
    assert seen["NET"] == "connected"
    # Even local keeps the server's own secrets out of the command.
    assert seen["ENV_CANARY"] == "0" and CANARY not in seen["READ_SERVER_ENV"]
