"""Harness of the durable e2e scenarios (not a pytest module): a counting
pass-through proxy in front of the e2e vLLM proxy, a schema per scenario,
a tenant `linch-server --config` that can be killed, stopped and restarted,
and event summaries. Results go to `E2E_DIR/results/durable/`.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import http.server
import json
import os
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

E2E_DIR = Path(os.environ.get("E2E_DIR", Path(tempfile.gettempdir()) / "linch-live-e2e"))
RESULTS = E2E_DIR / "results" / "durable"
RESULTS.mkdir(parents=True, exist_ok=True)
UPSTREAM = ("127.0.0.1", 18201)  # the e2e proxy; never the vLLM host itself
PG_DSN = os.environ["LINCH_TEST_PG_DSN"]
MODEL = os.environ.get("LINCH_TEST_VLLM_MODEL", "qwen3.6-35b-a3b")


def load() -> str:
    return open("/proc/loadavg").read().split()[0]


def log(*parts: Any) -> None:
    print(time.strftime("%H:%M:%S"), *parts, file=sys.stderr, flush=True)


def dump(name: str, obj: Any) -> None:
    (RESULTS / name).write_text(json.dumps(obj, indent=1, default=str))


# ---------------------------------------------------------------- proxy


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    proxy: CountingProxy

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self._forward(b"")

    def do_POST(self) -> None:
        n = int(self.headers.get("content-length") or 0)
        self._forward(self.rfile.read(n))

    def _forward(self, body: bytes) -> None:
        p = self.proxy
        rec: dict[str, Any] = {"t_arrive": time.time(), "path": self.path, "state": "arrived"}
        if self.command == "POST":
            with p.lock:
                p.calls.append(rec)
                rec["n"] = len(p.calls)
            try:
                rec["max_tokens"] = json.loads(body).get("max_tokens")
            except Exception:
                pass
            p.arrived.set()
            for cb in list(p.on_arrive):
                cb(rec)
            # A held request is never sent upstream until released.
            while p.hold and not p.released.wait(0.05):
                rec["state"] = "held"
            if rec.get("drop"):
                rec["state"] = "dropped"
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            rec["state"] = "forwarding"
        up = http.client.HTTPConnection(*UPSTREAM, timeout=300)
        try:
            headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in ("host", "connection", "content-length")
            }
            up.request(self.command, self.path, body=body or None, headers=headers)
            resp = up.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            rec["status"] = resp.status
            buf = b""
            while True:
                chunk = resp.read1(4096)
                if not chunk:
                    break
                buf += chunk
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
                if "t_first" not in rec:
                    rec["t_first"] = time.time()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            rec["state"] = "done"
            self._usage(rec, buf)
        except (BrokenPipeError, ConnectionResetError):
            rec["state"] = "client_gone"
        except Exception as error:  # upstream trouble
            rec["state"] = f"error:{error!r}"
        finally:
            rec["t_end"] = time.time()
            up.close()

    @staticmethod
    def _usage(rec: dict[str, Any], buf: bytes) -> None:
        for line in buf.decode(errors="replace").splitlines():
            if line.startswith("data:") and '"usage"' in line:
                try:
                    u = json.loads(line[5:]).get("usage")
                except Exception:
                    continue
                if u:
                    rec["prompt_tokens"] = u.get("prompt_tokens")
                    rec["completion_tokens"] = u.get("completion_tokens")


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class CountingProxy:
    """Pass-through to the e2e proxy that counts and can hold chat calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.hold = False
        self.released = threading.Event()
        self.arrived = threading.Event()
        self.on_arrive: list[Any] = []
        handler = type("H", (_Handler,), {"proxy": self})
        self.httpd = _Server(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset(self) -> None:
        with self.lock:
            self.calls.clear()
        self.hold = False
        self.released.clear()
        self.arrived.clear()
        self.on_arrive.clear()

    def close(self) -> None:
        self.released.set()
        self.httpd.shutdown()

    def n(self) -> int:
        return len(self.calls)

    def tokens(self) -> tuple[int, int]:
        p = sum(c.get("prompt_tokens") or 0 for c in self.calls)
        c = sum(c.get("completion_tokens") or 0 for c in self.calls)
        return p, c


# ---------------------------------------------------------------- postgres


@contextlib.contextmanager
def schema() -> Iterator[str]:
    """A DSN whose search_path is a fresh schema, dropped at the end."""
    import psycopg
    from psycopg import sql

    name = f"du_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            sep = "&" if "?" in PG_DSN else "?"
            yield f"{PG_DSN}{sep}options=-c%20search_path%3D{name}"
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def q(dsn: str, statement: str, *args: Any) -> list[tuple[Any, ...]]:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as c:
        return c.execute(statement, args).fetchall()  # type: ignore[arg-type]


# ---------------------------------------------------------------- configs


def cfg(
    dsn: str | None,
    base_url: str,
    ns: str,
    *,
    ttl: int | None = 3000,
    spec: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    c: dict[str, Any] = {
        "namespace": ns,
        "spec": {
            "model": MODEL,
            "max_output_tokens": 120,
            "max_turns": 6,
            "thinking": {"type": "disabled"},
            "system_prompt": "You are a terse test assistant. Follow instructions exactly.",
            **(spec or {}),
        },
        "provider": {"id": "vllm", "base_url": base_url},
        **extra,
    }
    if dsn is not None:
        stores: dict[str, Any] = {"type": "postgres", "dsn": dsn}
        if ttl is not None:
            stores["lease_ttl_ms"] = ttl
        c["stores"] = stores
    return c


# ---------------------------------------------------------------- events


def summarize(events: list[Any]) -> list[dict[str, Any]]:
    out = []
    for e in events:
        ev = e.event
        row: dict[str, Any] = {"seq": e.seq, "type": ev.type, "ts_ms": e.ts_ms}
        if ev.type == "usage":
            row["usage"] = {k: v for k, v in ev.usage.items() if isinstance(v, int)}
        elif ev.type == "model_call_delivery":
            row["turn"] = ev.turn_index
            row["attempt"] = ev.delivery_attempt_id
        elif ev.type == "model_call_receipt":
            row["disp"] = ev.disposition
            row["usage"] = ev.usage
        elif ev.type in ("tool_call_start", "tool_call_end"):
            row["tool"] = ev.tool_name
            row["id"] = ev.tool_use_id
        elif ev.type == "result":
            row["subtype"] = ev.subtype
            row["total_usage"] = dict(ev.total_usage)
            row["final"] = (ev.final_text or "")[:60]
        elif ev.type == "assistant":
            row["stop"] = ev.stop_reason
        out.append(row)
    return out


def types(rows: list[dict[str, Any]]) -> list[str]:
    return [r["type"] for r in rows]


def check_history(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The durable-history invariants: seq increasing, one result and last,
    unique tool starts, usage events reconcile with the result."""
    seqs = [r["seq"] for r in rows]
    results = [r for r in rows if r["type"] == "result"]
    starts = [r["id"] for r in rows if r["type"] == "tool_call_start"]
    usage = [r["usage"] for r in rows if r["type"] == "usage"]
    tot: dict[str, int] = {}
    for u in usage:
        for k, v in u.items():
            tot[k] = tot.get(k, 0) + v
    reported = results[-1]["total_usage"] if results else {}
    mismatch = {
        k: (v, reported.get(k)) for k, v in tot.items() if k in reported and reported[k] != v
    }
    return {
        "seq_strictly_increasing": seqs == sorted(set(seqs)),
        "one_result_and_last": len(results) == 1 and rows[-1]["type"] == "result",
        "tool_starts_unique": len(starts) == len(set(starts)),
        "assistants": sum(1 for r in rows if r["type"] == "assistant"),
        "usage_events": len(usage),
        "usage_sum": tot,
        "result_total_usage": {
            k: reported.get(k) for k in ("input_tokens", "output_tokens") if k in reported
        },
        "usage_vs_result_mismatch": mismatch,
    }


# ---------------------------------------------------------------- tenant server


class Srv:
    """A `linch-server --config` process: killable, stoppable, restartable."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        binary: str | None = None,
        env: dict[str, str] | None = None,
        directory: Path | None = None,
    ) -> None:
        self.dir = directory or Path(tempfile.mkdtemp(prefix="du-", dir="/tmp"))
        self.config = config
        self.path = self.dir / "server.json"
        self.path.write_text(json.dumps(config))
        self.binary = binary or os.environ["LINCH_SERVER_BIN"]
        self.env = {**os.environ, **(env or {})}
        self.proc: asyncio.subprocess.Process | None = None
        self.addresses: dict[str, str] = {}
        self.log_path = self.dir / "server.log"
        self.t_spawn = 0.0
        self.t_listen = 0.0

    @property
    def log(self) -> str:
        return self.log_path.read_text(errors="replace") if self.log_path.exists() else ""

    @property
    def pid(self) -> int:
        assert self.proc is not None
        return self.proc.pid

    async def start(self) -> Srv:
        want = {k for k in ("socket", "http") if k in self.config}
        sink = self.log_path.open("ab")
        self.t_spawn = time.time()
        self.proc = await asyncio.create_subprocess_exec(
            self.binary,
            "--config",
            str(self.path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.dir,
            env=self.env,
        )
        assert self.proc.stderr is not None
        stderr = self.proc.stderr
        got: set[str] = set()
        while want - got:
            line = await asyncio.wait_for(stderr.readline(), 30)
            assert line, f"server exited before it listened: {self.log}"
            sink.write(line)
            sink.flush()
            _, _, a = line.decode().strip().partition("listening on ")
            if a.startswith("unix:"):
                self.addresses["socket"] = a
                got.add("socket")
            elif a.startswith("ws://"):
                self.addresses["http"] = a
                got.add("http")
        self.t_listen = time.time()

        async def drain() -> None:
            while line := await stderr.readline():
                sink.write(line)
                sink.flush()
            sink.close()

        self._drain = asyncio.create_task(drain())
        return self

    async def kill(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.send_signal(signal.SIGKILL)
            await self.proc.wait()

    def sig(self, s: int) -> None:
        if self.proc is not None and self.proc.returncode is None:
            os.kill(self.proc.pid, s)

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), 20)
            except TimeoutError:
                await self.kill()

    def connect(self, token: str, which: str = "socket") -> Any:
        from linch_rust import Server

        return Server.connect(self.addresses[which], token=token)


def tenant(dsn: str | None, *, urls: list[str], **allow: Any) -> dict[str, Any]:
    a: dict[str, Any] = {
        "permission_modes": ["default", "accept_edits", "skip_dangerous"],
        "provider_base_urls": urls,
        "scripted_provider": True,
        "max_runs": 16,
        **allow,
    }
    if dsn:
        a["postgres_dsns"] = [dsn]
    return a


async def until(check: Any, *, timeout: float, every: float = 0.25, what: str = "it") -> Any:
    end = time.time() + timeout
    while True:
        v = check()
        if asyncio.iscoroutine(v):
            v = await v
        if v:
            return v
        if time.time() > end:
            raise AssertionError(f"{what} did not happen within {timeout} s")
        await asyncio.sleep(every)


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
