"""Support for the `long` live end-to-end suites: a body-logging tee in front of
the vLLM proxy, the results directory, and a few shared helpers.

The tee listens on a random 127.0.0.1 port and forwards `/v1/*` to the URL in
`LINCH_TEST_VLLM_URL` (the serialising proxy, never the GPU host). Every POST
is logged as one JSON line: the request body, the status, the usage the
response reported (prompt, completion and cached tokens), and the wall times.
Raw data of a test goes to `$E2E_RESULTS/long/` (default `/tmp/e2e-long`).
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

UPSTREAM = os.environ.get("LINCH_TEST_VLLM_URL") or os.environ.get("LINCH_LIVE_VLLM_URL") or ""
MODEL = os.environ.get("LINCH_TEST_VLLM_MODEL") or "qwen3.6-35b-a3b"
PG_DSN = os.environ.get("LINCH_TEST_PG_DSN", "")
RESULTS = Path(os.environ.get("E2E_RESULTS", "/tmp/e2e-long"))
RESULTS.mkdir(parents=True, exist_ok=True)

HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}  # fmt: skip


def usage_of(obj: Any) -> tuple[int | None, int | None, int | None] | None:
    u = obj.get("usage") if isinstance(obj, dict) else None
    if not isinstance(u, dict):
        return None
    details = u.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return u.get("prompt_tokens"), u.get("completion_tokens"), cached


class Tee:
    """A forwarding proxy that remembers every request body.

    Attributes:
        url: The `base_url` to give the provider (`http://127.0.0.1:<port>/v1`).
        records: One dict per POST: `ts`, `body` (the parsed request), `status`,
            `prompt_tokens`, `completion_tokens`, `cached_tokens`, `latency_s`.
    """

    def __init__(self, label: str) -> None:
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.path = RESULTS / f"{label}.tee.jsonl"
        self.path.write_text("")
        tee = self
        base = urllib.parse.urlparse(UPSTREAM)
        host, port = base.hostname or "127.0.0.1", base.port or 80

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a: Any) -> None:
                pass

            def do_GET(self) -> None:
                self._go(None)

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                self._go(self.rfile.read(n) if n else b"")

            def _go(self, body: bytes | None) -> None:
                t0 = time.time()
                rec: dict[str, Any] = {"ts": round(t0, 3), "path": self.path, "method": self.command}
                if body:
                    try:
                        rec["body"] = json.loads(body)
                    except ValueError:
                        rec["body_raw_len"] = len(body)
                conn = http.client.HTTPConnection(host, port, timeout=900)
                usage = None
                try:
                    hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
                    hdrs["Accept-Encoding"] = "identity"
                    hdrs["Connection"] = "close"
                    conn.request(self.command, self.path, body=body, headers=hdrs)
                    resp = conn.getresponse()
                    rec["status"] = resp.status
                    sse = "text/event-stream" in resp.getheader("Content-Type", "")
                    self.send_response(resp.status)
                    for k, v in resp.getheaders():
                        if k.lower() not in HOP:
                            self.send_header(k, v)
                    clen = resp.getheader("Content-Length")
                    chunked = sse or clen is None
                    if chunked:
                        self.send_header("Transfer-Encoding", "chunked")
                    else:
                        self.send_header("Content-Length", str(clen))
                    self.end_headers()
                    buf = b""
                    whole = b""
                    text: list[str] = []
                    while data := resp.read1(65536):
                        if chunked:
                            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                        else:
                            self.wfile.write(data)
                        self.wfile.flush()
                        if sse:
                            buf += data
                            *lines, buf = buf.split(b"\n")
                            for ln in lines:
                                if not ln.startswith(b"data:") or ln.strip() == b"data: [DONE]":
                                    continue
                                try:
                                    obj = json.loads(ln[5:].strip())
                                except ValueError:
                                    continue
                                usage = usage_of(obj) or usage
                                for ch in obj.get("choices") or []:
                                    piece = (ch.get("delta") or {}).get("content")
                                    if piece:
                                        text.append(piece)
                        else:
                            whole += data
                    if chunked:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    if whole:
                        try:
                            usage = usage_of(json.loads(whole)) or usage
                        except ValueError:
                            pass
                    if text:
                        rec["reply"] = "".join(text)
                except (BrokenPipeError, ConnectionResetError):
                    rec["client_aborted"] = True
                    self.close_connection = True
                finally:
                    conn.close()
                rec["latency_s"] = round(time.time() - t0, 3)
                if usage:
                    rec["prompt_tokens"], rec["completion_tokens"], rec["cached_tokens"] = usage
                if body is not None:
                    tee._add(rec)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _add(self, rec: dict[str, Any]) -> None:
        with self._lock:
            self.records.append(rec)
            with self.path.open("a") as f:
                f.write(json.dumps(rec) + "\n")

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def chat(self) -> list[dict[str, Any]]:
        """Only the chat completions, in arrival order."""
        return [r for r in self.records if "chat/completions" in r["path"] and "body" in r]


def unique(prefix: str = "e2el") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def dump(label: str, data: Any) -> Path:
    """Write `data` as JSON under the results directory and return the path."""
    path = RESULTS / f"{label}.json"
    path.write_text(json.dumps(data, indent=2, default=str))
    return path


def load() -> str:
    return Path("/proc/loadavg").read_text().split()[0]


def msg_text(message: dict[str, Any]) -> str:
    """The text of one chat message of a captured request (string or parts)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""
