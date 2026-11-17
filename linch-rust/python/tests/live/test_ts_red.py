"""RED tests: behaviour that differs from what the docs claim (product defects found by toolsec).

Each test states the claim it checks; a failing one is the evidence in the report.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess

from test_ts_bash import drop_client, wait_line
from ts_support import (
    fake_config,
    needs_server,
    plan_model,
    pytestmark,
    serving,
    skip_dangerous,
    tenant,
    until_async,
    workspace,
)

__all__ = ["pytestmark"]

MARKER = "sleep 3171"


def sleepers() -> list[str]:
    done = subprocess.run(["pgrep", "-af", f"^{MARKER}$"], capture_output=True, text=True)
    return [line for line in done.stdout.splitlines() if line]


@needs_server
async def test_a_killed_client_does_not_leave_its_remote_background_command_running() -> None:
    """`SubprocessHost` docstring: "nothing outlives the server that asked for it".

    Here the client process is SIGKILLed with a background command in flight; the host (inside that
    process) cannot act, and its command, started in its own session, keeps running.
    """
    model = await plan_model([("Bash", {"command": MARKER, "run_in_background": True})]).start()
    try:
        with workspace() as root:
            tenants = {
                "acme": tenant(
                    "tok-acme-0001",
                    exec_tools=True,
                    permission_modes=["default", "skip_dangerous"],
                    workspace_roots=[str(root)],
                    provider_base_urls=[model.url],
                    execution=[{"backend": "remote"}],
                )
            }
            async with serving(tenants) as proc:
                config = fake_config(
                    model,
                    namespace="acme",
                    workspace=str(root),
                    permissions=skip_dangerous(root),
                    execution={"backend": "remote"},
                )
                client = drop_client(proc.address, "tok-acme-0001", config, root, "remote")
                try:
                    await wait_line(client, "RUN_ENDED")
                    await asyncio.sleep(0.5)
                    assert sleepers(), "the remote command never started"
                    client.send_signal(signal.SIGKILL)
                    client.wait()
                    gone = await until_async(lambda: not sleepers(), 8)
                    left = sleepers()
                finally:
                    client.kill()
                    subprocess.run(["pkill", "-f", f"^{MARKER}$"], check=False)  # my own sleeper
                assert gone is not None, f"the command outlived its killed client: {left}"
    finally:
        await model.stop()
