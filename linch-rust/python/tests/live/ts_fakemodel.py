"""A programmable OpenAI-compatible gateway for the toolsec live tests.

It serves `POST /v1/chat/completions` (streaming) and answers each request by calling
`handler(request)`, which sees the parsed body, its headers and a call counter and returns one of:

- `("text", "words")`: a text answer;
- `("tools", [("Bash", {"command": "true"}), ...])`: tool calls;
- `("http", 400, b"body")`: an HTTP error answer (the body is sent as is).

Every request is kept in `.requests` (headers lower-cased, body parsed). `GET /v1/models` answers
one
model. No LLM is involved: the scenario is the handler.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Answer = tuple[Any, ...]


@dataclass
class Seen:
    path: str
    headers: dict[str, str]
    body: dict[str, Any]
    n: int


@dataclass
class FakeModel:
    handler: Callable[[Seen], Answer]
    port: int = 0
    requests: list[Seen] = field(default_factory=list[Seen])
    _server: asyncio.Server | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    async def start(self) -> FakeModel:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                method, path, _ = lines[0].split(" ", 2)
                headers = {}
                for line in lines[1:]:
                    if ":" in line:
                        key, _, value = line.partition(":")
                        headers[key.strip().lower()] = value.strip()
                length = int(headers.get("content-length", "0"))
                raw = await reader.readexactly(length) if length else b""
                if method == "GET":
                    body = json.dumps(
                        {"object": "list", "data": [{"id": "fake", "object": "model"}]}
                    )
                    self._reply(writer, 200, body.encode(), "application/json")
                    await writer.drain()
                    continue
                parsed = json.loads(raw or b"{}")
                seen = Seen(path, headers, parsed, len(self.requests) + 1)
                self.requests.append(seen)
                answer = self.handler(seen)
                if answer[0] == "http":
                    self._reply(writer, answer[1], answer[2], "text/plain")
                else:
                    self._stream(writer, answer, parsed.get("model", "fake"))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    @staticmethod
    def _reply(writer: asyncio.StreamWriter, status: int, body: bytes, kind: str) -> None:
        head = (
            f"HTTP/1.1 {status} X\r\nContent-Type: {kind}\r\nContent-Length: {len(body)}\r\n"
            "Connection: keep-alive\r\n\r\n"
        )
        writer.write(head.encode() + body)

    @staticmethod
    def _stream(writer: asyncio.StreamWriter, answer: Answer, model: str) -> None:
        def chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
            body = {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return b"data: " + json.dumps(body).encode() + b"\n\n"

        parts = [chunk({"role": "assistant", "content": ""})]
        if answer[0] == "text":
            parts.append(chunk({"content": answer[1]}))
            parts.append(chunk({}, "stop"))
        else:
            for index, (name, args) in enumerate(answer[1]):
                call = {
                    "index": index,
                    "id": f"call_{index}_{abs(hash(json.dumps(args, sort_keys=True))) % 10**8}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                parts.append(chunk({"tool_calls": [call]}))
            parts.append(chunk({}, "tool_calls"))
        usage = {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [],
            "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
        }
        parts.append(b"data: " + json.dumps(usage).encode() + b"\n\n")
        parts.append(b"data: [DONE]\n\n")
        body = b"".join(parts)
        head = (
            "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            f"Content-Length: {len(body)}\r\nConnection: keep-alive\r\n\r\n"
        )
        writer.write(head.encode() + body)


def last_tool_text(body: dict[str, Any]) -> str:
    """The text of the newest `tool` message of a request."""
    for message in reversed(body.get("messages", [])):
        if message.get("role") == "tool":
            content = message.get("content")
            if isinstance(content, list):
                return "".join(p.get("text", "") for p in content)
            return str(content)
    return ""
