"""Support of the `test_live_e2e_core_*` suites (live end-to-end of the core features).

Every provider call goes through `Forwarder`, an in-process pass-through in front of
`LINCH_TEST_VLLM_URL` (the shared proxy): it records, per request, the arrival time,
the end time, the request body and the usage the server reported. That is the
evidence for what Linch put on the wire (`tool_choice`, `reasoning_effort`,
`temperature`) and for when a call started, which the shared proxy log cannot give
(it has no request id and other agents write to it).

Raw data goes to `E2E_RESULTS` (default `e2e/results/core`) as JSON lines.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import http.client
import json
import os
import tempfile
import threading
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TypeVar

import pytest
from live_core_support import (
    LIVE_KIND,
    PG_DSN,
    Trace,
    drive,
    live_server,
)

from linch_rust import Server

T = TypeVar("T")

UPSTREAM = os.environ.get("LINCH_TEST_VLLM_URL") or os.environ.get("LINCH_LIVE_VLLM_URL") or ""
MODEL = (
    os.environ.get("LINCH_LIVE_VLLM_MODEL")
    or os.environ.get("LINCH_TEST_VLLM_MODEL")
    or "qwen3.6-35b-a3b"
)
RESULTS = Path(
    os.environ.get("E2E_RESULTS", Path(tempfile.gettempdir()) / "linch-live-e2e" / "core")
)

needs_e2e = pytest.mark.skipif(
    not (UPSTREAM and PG_DSN), reason="LINCH_TEST_VLLM_URL and LINCH_TEST_PG_DSN are required"
)


def dump(name: str, row: Any) -> None:
    """Append one JSON line to `RESULTS/<name>.jsonl`."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    with (RESULTS / f"{name}.jsonl").open("a") as out:
        out.write(json.dumps(row, default=str) + "\n")


def trace_rows(trace: Trace) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for env in trace.envelopes:
        body = dataclasses.asdict(env.event) if dataclasses.is_dataclass(env.event) else {}
        rows.append({"seq": env.seq, "ts_ms": env.ts_ms, "type": env.event.type, **body})
    return rows


class Forwarder:
    """A recording pass-through to the vLLM proxy on a random local port."""

    def __init__(self, upstream: str = UPSTREAM) -> None:
        parts = urllib.parse.urlsplit(upstream)
        self.host, self.port, self.prefix = (
            parts.hostname or "127.0.0.1",
            parts.port or 80,
            parts.path,
        )
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        forwarder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _forward(self) -> None:
                arrive = time.time()
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                path = self.path
                if forwarder.prefix and not path.startswith(forwarder.prefix):
                    path = forwarder.prefix.rstrip("/") + "/" + path.lstrip("/")
                record: dict[str, Any] = {
                    "method": self.command,
                    "path": self.path,
                    "arrive": arrive,
                }
                try:
                    record["body"] = json.loads(raw) if raw else None
                except ValueError:
                    record["body"] = None
                conn = http.client.HTTPConnection(forwarder.host, forwarder.port, timeout=600)
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in {"host", "connection", "content-length"}
                }
                try:
                    conn.request(self.command, path, body=raw or None, headers=headers)
                    resp = conn.getresponse()
                    record["status"] = resp.status
                    self.send_response(resp.status)
                    for key, value in resp.getheaders():
                        if key.lower() not in {"transfer-encoding", "connection", "content-length"}:
                            self.send_header(key, value)
                    self.end_headers()
                    tail = b""
                    first = True
                    while True:
                        chunk = resp.read1(65536)  # type: ignore[attr-defined]
                        if not chunk:
                            break
                        if first:
                            record["first_byte"] = time.time()
                            first = False
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        tail = (tail + chunk)[-8192:]
                    record["usage"] = _usage_of(tail)
                except Exception as error:  # noqa: BLE001
                    record["error"] = repr(error)
                finally:
                    record["end"] = time.time()
                    conn.close()
                    with forwarder._lock:
                        forwarder.records.append(record)

            do_GET = _forward
            do_POST = _forward

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> Forwarder:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def chat(self) -> list[dict[str, Any]]:
        """The recorded chat-completion requests, in arrival order."""
        with self._lock:
            rows = [r for r in self.records if r["path"].endswith("/chat/completions")]
        return sorted(rows, key=lambda r: r["arrive"])

    def mark(self) -> int:
        return len(self.chat())


def _usage_of(tail: bytes) -> dict[str, Any] | None:
    usage = None
    for line in tail.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("data:") and "usage" in line:
            try:
                obj = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("usage"):
                usage = obj["usage"]
    return usage


@pytest.fixture(scope="module")
def fwd() -> Any:
    forwarder = Forwarder().start()
    yield forwarder
    forwarder.stop()


def pg() -> dict[str, Any]:
    assert PG_DSN
    return {"type": "postgres", "dsn": PG_DSN}


def spec(**extra: Any) -> dict[str, Any]:
    """A small vLLM spec: thinking off, 400 output tokens; `extra` overrides any field."""
    base: dict[str, Any] = {
        "model": MODEL,
        "max_output_tokens": 400,
        "thinking": {"type": "disabled"},
        "call_timeout_ms": 120_000,
        "system_prompt": "You are a terse test agent. Follow tool instructions exactly.",
    }
    base.update(extra)
    return base


def config(fwd: Forwarder | None, spec_: dict[str, Any] | None = None, **top: Any) -> Any:
    """An agent config on Postgres with a fresh namespace, through the forwarder."""
    url = fwd.url if fwd is not None else UPSTREAM
    cfg: dict[str, Any] = {
        "namespace": f"e2ecore-{uuid.uuid4().hex[:8]}",
        "spec": spec_ if spec_ is not None else spec(),
        "provider": {"id": LIVE_KIND, "base_url": url},
        "stores": pg(),
    }
    cfg.update(top)
    return cfg


@asynccontextmanager
async def opened(
    cfg: Any, *, tools: tuple[Any, ...] = (), callbacks: tuple[Any, ...] = ()
) -> AsyncIterator[tuple[Server, Any]]:
    async with live_server() as server:
        agent = await server.open_agent(cfg, tools=list(tools), callbacks=list(callbacks))
        yield server, agent


async def run_to_end(
    name: str,
    agent: Any,
    prompt: str,
    *,
    session: str = "s1",
    fwd: Forwarder | None = None,
    **run_kwargs: Any,
) -> Trace:
    """Run `prompt`, drive it to its result, and dump the events and the wire records."""
    mark = fwd.mark() if fwd is not None else 0
    started = time.time()
    run = await agent.run(session, prompt, **run_kwargs)
    trace = await drive(agent, run)
    dump(f"{name}.events", {"started": started, "ended": time.time(), "events": trace_rows(trace)})
    if fwd is not None:
        dump(f"{name}.wire", fwd.chat()[mark:])
    return trace


def attempts(
    name: str, tries: int = 2
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Run a model-driven scenario up to `tries` times, recording every attempt.

    Unlike `retry_once`, a first failure is written to `attempts.jsonl`, so a model
    behaviour rerun is visible in the evidence.
    """

    def wrap(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def inner(*args: Any, **kwargs: Any) -> T:
            last: BaseException | None = None
            for attempt in range(1, tries + 1):
                try:
                    out = await fn(*args, **kwargs)
                    dump("attempts", {"test": name, "attempt": attempt, "ok": True})
                    return out
                except (AssertionError, KeyError, IndexError, ValueError) as error:
                    last = error
                    dump(
                        "attempts",
                        {"test": name, "attempt": attempt, "ok": False, "error": str(error)[:600]},
                    )
            assert last is not None
            raise last

        return inner

    return wrap


def sleeper(seconds: float) -> Callable[[], Awaitable[None]]:
    async def sleep() -> None:
        await asyncio.sleep(seconds)

    return sleep
