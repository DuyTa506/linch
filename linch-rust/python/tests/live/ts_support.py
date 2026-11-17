"""Shared pieces of the `test_ts_*.py` live suites (tools, sandbox, remote execution, MCP, tenants).

Everything here drives a real `linch-server` process. Most scenarios use the `scripted`
provider (no LLM); the two model tests use the vLLM proxy. Raw observations are appended as
JSON lines to `$TOOLSEC_RESULTS/<area>.jsonl` so the report can cite them.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import pytest
from ts_fakemodel import FakeModel, Seen, last_tool_text

from linch_rust import (
    Agent,
    Allow,
    Envelope,
    PermissionRequestEvent,
    ResultEvent,
    Run,
    Server,
    SuspendedEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    find_server,
)
from linch_rust.scripted import MODEL, text_turn, tool_turn

pytestmark = pytest.mark.live

RESULTS = Path(
    os.environ.get(
        "TOOLSEC_RESULTS", "/home/aiqk3/Desktop/duy_dev/linch-agent-scratch/e2e/results/toolsec"
    )
)
PG_DSN = os.environ.get("LINCH_TEST_PG_DSN", "")
VLLM_URL = os.environ.get("LINCH_TEST_VLLM_URL", "")
VLLM_MODEL = os.environ.get("LINCH_TEST_VLLM_MODEL", "qwen3.6-35b-a3b")
HERE = Path(__file__).resolve().parent
MCP_SERVER = HERE / "ts_mcp_server.py"

needs_server = pytest.mark.skipif(
    not os.environ.get("LINCH_SERVER_BIN"), reason="LINCH_SERVER_BIN is not set"
)
needs_vllm = pytest.mark.skipif(not VLLM_URL, reason="LINCH_TEST_VLLM_URL is not set")

# Secrets the server holds in its own environment; no command, error or log may show them.
CANARY = "TSCANARY-" + uuid.uuid4().hex


def record(area: str, name: str, **data: Any) -> None:
    """Append one observation to `$TOOLSEC_RESULTS/<area>.jsonl`."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    row = {
        "t": round(time.time(), 3),
        "test": name,
        "binary": os.path.basename(os.environ.get("LINCH_SERVER_BIN", "")),
        "load1": os.getloadavg()[0],
        **data,
    }
    with (RESULTS / f"{area}.jsonl").open("a") as handle:
        handle.write(json.dumps(row, default=str) + "\n")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def server_env(**extra: str) -> dict[str, str]:
    """A server environment that carries secrets a command must never see."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "TERM": "xterm",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LC_CTYPE": "C.UTF-8",
        "USER": os.environ.get("USER", "aiqk3"),
        "SHELL": "/bin/bash",
        "TMPDIR": "/tmp",
        "LINCH_TEST_PG_DSN": PG_DSN or "postgres://u:p@h/db-canary",
        "TS_CANARY": CANARY,
        "DEEPSEEK_API_KEY": "sk-" + CANARY,
    }
    env.update(extra)
    return env


def children(pid: int) -> list[int]:
    """Direct children of `pid`, as `pgrep -P` lists them."""
    done = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
    return [int(p) for p in done.stdout.split()]


def descendants(pid: int) -> list[int]:
    found: list[int] = []
    for child in children(pid):
        found.append(child)
        found.extend(descendants(child))
    return found


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return False
    return stat.read_text().rsplit(")", 1)[-1].split()[0] != "Z"


def rss_kb(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


async def until_async(check: Any, timeout: float, every: float = 0.1) -> float | None:
    """Seconds until `check()` is truthy, or None past `timeout`."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if check():
            return time.monotonic() - start
        await asyncio.sleep(every)
    return None


def tenant(token: str, **allow: Any) -> dict[str, Any]:
    return {"tokens": [{"value": token}], "allow": allow}


class ServerProc:
    """A `linch-server --config` process on a private socket (and optionally HTTP).

    Attributes:
        address: `unix:<path>` of its socket.
        ws: `ws://127.0.0.1:<port>/rpc` when `http` is on.
    """

    def __init__(
        self,
        tenants: dict[str, Any],
        *,
        env: dict[str, str] | None = None,
        http: bool = False,
        extra: dict[str, Any] | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="ts-", dir="/tmp"))
        self.config: dict[str, Any] = {"socket": str(self.dir / "s.sock"), "tenants": tenants}
        self.port = free_port() if http else 0
        if http:
            self.config["http"] = f"127.0.0.1:{self.port}"
        self.config.update(extra or {})
        self.path = self.dir / "server.json"
        self.path.write_text(json.dumps(self.config))
        self.env = env if env is not None else server_env()
        self.cwd = cwd or self.dir
        self.log_path = self.dir / "stderr.log"
        self.process: subprocess.Popen[bytes] | None = None
        self.address = f"unix:{self.dir / 's.sock'}"
        self.ws = f"ws://127.0.0.1:{self.port}/rpc" if http else ""

    @property
    def pid(self) -> int:
        assert self.process is not None
        return self.process.pid

    @property
    def log(self) -> str:
        return self.log_path.read_text(errors="replace") if self.log_path.exists() else ""

    async def start(self) -> ServerProc:
        sink = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [find_server(), "--config", str(self.path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=sink,
            cwd=self.cwd,
            env=self.env,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"server exited {self.process.returncode}: {self.log}")
            if "listening on" in self.log and (not self.port or "ws://" in self.log):
                return self
            await asyncio.sleep(0.05)
        raise AssertionError(f"server never listened: {self.log}")

    def kill(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGKILL)
            self.process.wait()

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.kill()

    def connect(self, token: str) -> Server:
        return Server.connect(self.address, token=token)

    def cleanup(self) -> None:
        self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


@asynccontextmanager
async def serving(tenants: dict[str, Any], **kwargs: Any) -> AsyncIterator[ServerProc]:
    proc = ServerProc(tenants, **kwargs)
    await proc.start()
    try:
        yield proc
    finally:
        proc.cleanup()


@contextmanager
def workspace() -> Any:
    root = Path(tempfile.mkdtemp(prefix="tsws-", dir="/tmp")).resolve()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def scripted_config(
    turns: list[dict[str, Any]],
    *,
    namespace: str | None = None,
    spec: dict[str, Any] | None = None,
    **top: Any,
) -> Any:
    config: dict[str, Any] = {
        "namespace": namespace or f"ts-{uuid.uuid4().hex[:8]}",
        "spec": {"model": MODEL, "max_turns": 50, **(spec or {})},
        "provider": {"id": "scripted", "turns": turns},
    }
    config.update(top)
    return config


def bash_turns(*commands: str | dict[str, Any], final: str = "done") -> list[dict[str, Any]]:
    """One `Bash` call per command (a str, or the full input dict), then a text answer."""
    turns = []
    for index, command in enumerate(commands):
        payload = command if isinstance(command, dict) else {"command": command}
        turns.append(tool_turn(f"c{index}", "Bash", payload))
    turns.append(text_turn(final))
    return turns


def plan_model(steps: list[Any], final: str = "done") -> FakeModel:
    """A fake model that follows `steps`, one per request.

    A step is `(tool, args)`, a list of those, or a function of the previous tool result text that
    returns one; after the last step it answers `final`.
    """

    def handler(seen: Seen) -> Any:
        index = seen.n - 1
        if index >= len(steps):
            return ("text", final)
        step = steps[index]
        if callable(step):
            step = step(last_tool_text(seen.body))
        return ("tools", step if isinstance(step, list) else [step])

    return FakeModel(handler)


def fake_config(
    model: FakeModel,
    *,
    namespace: str | None = None,
    spec: dict[str, Any] | None = None,
    **top: Any,
) -> Any:
    """An agent config whose provider is `model` (vLLM wire format, no LLM behind it)."""
    config: dict[str, Any] = {
        "namespace": namespace or f"ts-{uuid.uuid4().hex[:8]}",
        "spec": {
            "model": "fake",
            "max_turns": 50,
            "thinking": {"type": "disabled"},
            **(spec or {}),
        },
        "provider": {"id": "vllm", "base_url": model.url},
    }
    config.update(top)
    return config


def text_of(end: ToolCallEndEvent) -> str:
    return "".join(part.get("text", "") for part in end.result.get("content", []))


async def finish(
    agent: Agent, run: Run, *, approve: bool = True, rounds: int = 6
) -> list[Envelope]:
    """Every envelope of a run to its result, approving every ask on the way."""
    seen: list[Envelope] = []
    for _ in range(rounds):
        asks: dict[str, Any] = {}
        async for envelope in run:
            seen.append(envelope)
            if isinstance(envelope.event, PermissionRequestEvent):
                for request in envelope.event.requests:
                    asks[request["tool_use_id"]] = Allow()
        if not isinstance(seen[-1].event, SuspendedEvent) or not approve:
            return seen
        run = await agent.approve(run.id, asks)
    raise AssertionError("run kept suspending")


def ends(envelopes: list[Envelope], name: str | None = None) -> list[ToolCallEndEvent]:
    return [
        e.event
        for e in envelopes
        if isinstance(e.event, ToolCallEndEvent) and name in (None, e.event.tool_name)
    ]


def starts(envelopes: list[Envelope], name: str | None = None) -> list[ToolCallStartEvent]:
    return [
        e.event
        for e in envelopes
        if isinstance(e.event, ToolCallStartEvent) and name in (None, e.event.tool_name)
    ]


def result_of(envelopes: list[Envelope]) -> ResultEvent:
    results = [e.event for e in envelopes if isinstance(e.event, ResultEvent)]
    assert results, [e.event.type for e in envelopes][-6:]
    return results[-1]


def skip_dangerous(root: Path) -> dict[str, Any]:
    return {"mode": "skip_dangerous", "project_root": str(root)}


def pctl(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


__all__ = [
    "CANARY",
    "MODEL",
    "PG_DSN",
    "VLLM_MODEL",
    "VLLM_URL",
    "MCP_SERVER",
    "ServerProc",
    "alive",
    "FakeModel",
    "Seen",
    "bash_turns",
    "fake_config",
    "last_tool_text",
    "plan_model",
    "children",
    "descendants",
    "ends",
    "finish",
    "needs_server",
    "needs_vllm",
    "pctl",
    "record",
    "result_of",
    "rss_kb",
    "scripted_config",
    "server_env",
    "serving",
    "skip_dangerous",
    "starts",
    "tenant",
    "text_of",
    "text_turn",
    "tool_turn",
    "until_async",
    "workspace",
]
