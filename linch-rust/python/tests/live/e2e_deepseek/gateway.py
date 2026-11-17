"""Counting gateway in front of the DeepSeek API, for the live e2e runs.

- Forwards only `POST .../chat/completions` to https://api.deepseek.com (the Authorization header is passed
  through and never stored); everything else is 404.
- Header NAMES are logged, never values (the one probe header's value is only compared, not stored).
- Refuses locally (HTTP 400, not forwarded, not counted) a request with no `max_tokens`, one over the cap,
  or any request once the persisted forwarded count reached the limit.
- `x-e2e-mode: echo400` answers a 400 whose body quotes the value of `x-e2e-probe` (scrub test); not forwarded.
- Control file (JSON, re-read per request): {"mock": bool, "text": str, "args": {tool: {..}},
  "max_tokens_cap": int, "limit": int}. With `mock` the reply is a canned SSE stream (not forwarded, not
  counted); a forced named tool_choice (or "required" with a tool) is answered with a tool call.
- Every forwarded request body is stored in `<dir>/bodies/NNN.json` (bodies hold no credentials).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

UPSTREAM = "api.deepseek.com"


class Gateway:
    def __init__(self, directory: Path, counter: Path | None = None) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "bodies").mkdir(exist_ok=True)
        self.control_path = self.dir / "control.json"
        self.log_path = self.dir / "gateway.jsonl"
        # The forwarded-request count persists across invocations in the live directory.
        self.counter = counter or (self.dir / "count.txt")
        self.lock = threading.Lock()
        self.inflight = 0
        self.seq = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        if not self.control_path.exists():
            self.control(mock=True)

    # -- control -----------------------------------------------------------------------------
    def control(self, **fields: Any) -> dict[str, Any]:
        current = self._read_control()
        current.update(fields)
        self.control_path.write_text(json.dumps(current))
        return current

    def _read_control(self) -> dict[str, Any]:
        try:
            return json.loads(self.control_path.read_text())
        except (OSError, ValueError):
            return {}

    def forwarded(self) -> int:
        try:
            return int(self.counter.read_text().strip())
        except (OSError, ValueError):
            return 0

    def start(self) -> Gateway:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def records(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines() if line]

    def log(self, record: dict[str, Any]) -> None:
        with self.lock, self.log_path.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    # -- request handling ---------------------------------------------------------------------
    def _handler(self) -> type[BaseHTTPRequestHandler]:
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # the body is delimited by closing the connection

            def log_message(self, *_: Any) -> None:
                pass

            def do_GET(self) -> None:
                self._plain(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                gateway.handle(self)

            def _plain(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("connection", "close")
                self.end_headers()
                self.wfile.write(data)

        return Handler

    def handle(self, request: BaseHTTPRequestHandler) -> None:
        arrival = time.time()
        path = request.path.split("?")[0]
        length = int(request.headers.get("content-length") or 0)
        raw = request.rfile.read(length)
        if not path.endswith("/chat/completions"):
            request._plain(404, {"error": {"message": "not found"}})  # type: ignore[attr-defined]
            return
        names = sorted({name.lower() for name in request.headers})
        probe = request.headers.get("x-e2e-probe")
        control = self._read_control()
        try:
            body = json.loads(raw)
        except ValueError:
            request._plain(400, {"error": {"message": "bad json"}})  # type: ignore[attr-defined]
            return
        base: dict[str, Any] = {
            "arrival": arrival,
            "path": path,
            "header_names": names,
            "probe_header_present": probe is not None,
            "model": body.get("model"),
            "max_tokens": body.get("max_tokens"),
            "tool_choice": body.get("tool_choice"),
            "tools": [t.get("function", {}).get("name") for t in body.get("tools") or []],
            "thinking": body.get("thinking"),
            "reasoning_effort": body.get("reasoning_effort"),
            "has_response_format": "response_format" in body,
            "stream": body.get("stream"),
            "messages": len(body.get("messages") or []),
        }
        if request.headers.get("x-e2e-mode") == "echo400":
            message = f"Invalid request: header x-e2e-probe rejected with value {probe}"
            data = {"error": {"message": message, "type": "invalid_request_error", "param": None, "code": "invalid_request_error"}}
            self.log({**base, "kind": "echo400", "status": 400})
            request._plain(400, data)  # type: ignore[attr-defined]
            return
        cap = int(control.get("max_tokens_cap", 300))
        mt = body.get("max_tokens")
        if mt is None or int(mt) > cap:
            self.log({**base, "kind": "refused_max_tokens", "status": 400, "cap": cap})
            request._plain(400, {"error": {"message": f"gateway: max_tokens {mt} not allowed (cap {cap})"}})  # type: ignore[attr-defined]
            return
        if control.get("mock", True):
            self._mock(request, body, control, base, raw)
            return
        limit = int(control.get("limit", 30))
        with self.lock:
            count = self.forwarded()
            if count >= limit:
                over = True
            else:
                over = False
                self.counter.write_text(str(count + 1))
                self.seq = count + 1
                seq = self.seq
        if over:
            self.log({**base, "kind": "refused_limit", "status": 400})
            request._plain(400, {"error": {"message": "gateway: request limit reached"}})  # type: ignore[attr-defined]
            return
        (self.dir / "bodies" / f"{seq:03d}.json").write_text(json.dumps(body))
        self._forward(request, raw, base, seq)

    def _forward(self, request: BaseHTTPRequestHandler, raw: bytes, base: dict[str, Any], seq: int) -> None:
        auth = request.headers.get("authorization", "")
        headers = {
            "authorization": auth,
            "content-type": "application/json",
            "accept": request.headers.get("accept", "*/*"),
            "accept-encoding": "identity",
        }
        with self.lock:
            self.inflight += 1
            inflight_at_start = self.inflight
        t_start = time.time()
        record: dict[str, Any] = {**base, "kind": "forwarded", "seq": seq, "t_start": t_start, "inflight_at_start": inflight_at_start}
        text = b""
        status = 0
        first_byte: float | None = None
        try:
            conn = http.client.HTTPSConnection(UPSTREAM, timeout=120)
            conn.request("POST", "/chat/completions", body=raw, headers=headers)
            response = conn.getresponse()
            status = response.status
            request.send_response(status)
            request.send_header("content-type", response.getheader("content-type", "application/json"))
            request.send_header("connection", "close")
            request.end_headers()
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                if first_byte is None:
                    first_byte = time.time()
                text += chunk
                request.wfile.write(chunk)
                request.wfile.flush()
            conn.close()
        except Exception as error:  # noqa: BLE001
            record["gateway_error"] = type(error).__name__
        finally:
            with self.lock:
                self.inflight -= 1
        record.update(status=status, t_first_byte=first_byte, t_end=time.time())
        scrub = auth.removeprefix("Bearer ").strip()
        decoded = text.decode("utf-8", "replace")
        if scrub:
            decoded = decoded.replace(scrub, "<key>")
        record.update(parse_response(decoded, status))
        self.log(record)

    def _mock(self, request: BaseHTTPRequestHandler, body: dict[str, Any], control: dict[str, Any], base: dict[str, Any], raw: bytes) -> None:
        t_start = time.time()
        with self.lock:
            self.mock_seq = getattr(self, "mock_seq", 0) + 1
            mseq = self.mock_seq
        (self.dir / "bodies" / f"mock-{mseq:03d}.json").write_text(json.dumps(body))
        time.sleep(float(control.get("delay", 0)))
        text = control.get("text", "ok")
        choice = body.get("tool_choice")
        tools = [t["function"]["name"] for t in body.get("tools") or []]
        call: str | None = None
        if isinstance(choice, dict) and choice.get("function", {}).get("name"):
            call = choice["function"]["name"]
        elif choice == "required" and tools:
            call = tools[0]
        # A compaction summary request must be answered with text even under a forced choice.
        usage = {
            "prompt_tokens": len(raw) // 4,
            "completion_tokens": max(1, len(text) // 4),
            "total_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": len(raw) // 4,
        }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        chunks: list[dict[str, Any]] = []
        mk = lambda delta, finish=None: {  # noqa: E731
            "id": "mock",
            "object": "chat.completion.chunk",
            "model": body.get("model"),
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        chunks.append(mk({"role": "assistant", "content": ""}))
        if call is not None:
            args = json.dumps((control.get("args") or {}).get(call, {}))
            chunks.append(mk({"tool_calls": [{"index": 0, "id": "call_mock", "type": "function", "function": {"name": call, "arguments": ""}}]}))
            chunks.append(mk({"tool_calls": [{"index": 0, "function": {"arguments": args}}]}))
            chunks.append(mk({}, "tool_calls"))
        else:
            for i in range(0, len(text), 400):
                chunks.append(mk({"content": text[i : i + 400]}))
            chunks.append(mk({}, "stop"))
        chunks.append({"id": "mock", "object": "chat.completion.chunk", "model": body.get("model"), "choices": [], "usage": usage})
        payload = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        request.send_response(200)
        request.send_header("content-type", "text/event-stream")
        request.send_header("connection", "close")
        request.end_headers()
        request.wfile.write(payload.encode())
        self.log({**base, "kind": "mock", "mock_seq": mseq, "status": 200, "t_start": t_start, "t_end": time.time(), "mock_call": call, "text_chars": len(text), "last_user": last_text(body)[:120]})


def last_text(body: dict[str, Any]) -> str:
    messages = body.get("messages") or []
    if not messages:
        return ""
    content = messages[-1].get("content")
    if isinstance(content, str):
        return content
    return json.dumps(content)


def parse_response(decoded: str, status: int) -> dict[str, Any]:
    """Raw `usage`, model and finish reason of an SSE or JSON body; the error body otherwise."""
    out: dict[str, Any] = {}
    if status != 200:
        out["error_body"] = decoded[:1500]
        return out
    if decoded.lstrip().startswith("{"):
        try:
            whole = json.loads(decoded)
            choice = (whole.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            out.update(usage=whole.get("usage"), finish_reason=choice.get("finish_reason"), response_model=whole.get("model"),
                       content_chars=len(message.get("content") or ""), reasoning_chars=len(message.get("reasoning_content") or ""),
                       tool_calls=[(c.get("function") or {}).get("name") for c in message.get("tool_calls") or []])
            return out
        except ValueError:
            pass
    usage = None
    finish = None
    model = None
    content_chars = 0
    reasoning_chars = 0
    tool_names: list[str] = []
    for line in decoded.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        model = chunk.get("model", model)
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            delta = choice.get("delta") or {}
            content_chars += len(delta.get("content") or "")
            reasoning_chars += len(delta.get("reasoning_content") or "")
            for call in delta.get("tool_calls") or []:
                name = (call.get("function") or {}).get("name")
                if name:
                    tool_names.append(name)
    out.update(usage=usage, finish_reason=finish, response_model=model, content_chars=content_chars, reasoning_chars=reasoning_chars, tool_calls=tool_names)
    return out


def message_hashes(body: dict[str, Any]) -> list[str]:
    return [hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()[:12] for m in body.get("messages") or []]
