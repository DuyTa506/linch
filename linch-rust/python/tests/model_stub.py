"""A stand-in model server for the long-haul harness: an OpenAI-compatible
`/v1/chat/completions` that answers from the request alone.

It keeps no state of its own. What it does next comes from the task ledger the
request carries, and from the last tool result in the conversation, so a run
that was killed, resumed, compacted or started again in a fresh session gets
the same answer the uninterrupted run would. That is the property the harness
checks the agent for: whatever it needs to continue is outside the context.

The scenario: build `/report.md` from `/in/a.txt`, `/in/b.txt` and `/in/c.txt`.
Four tasks (a skeleton, then one per file); each is done with a tool call and
then marked completed; a last edit removes the end marker.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

MARKER = "<!--END-->"
SUBJECTS = [
    "Create report.md skeleton",
    "Summarise a.txt",
    "Summarise b.txt",
    "Summarise c.txt",
]
SUMMARY_PROMPT = "Summarize the conversation so far"


def text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def ledger_of(messages: list[dict[str, Any]]) -> tuple[list[tuple[int, str, str]], int] | None:
    """`(open tasks as (id, status, subject), completed count)`, from the
    ledger block the request carries; `None` when it carries none."""
    for message in reversed(messages):
        text = text_of(message)
        if "Task ledger:" not in text:
            continue
        found = re.findall(r"^#(\d+) \[(\w+)\] (.*)$", text, re.MULTILINE)
        done = re.search(r"\((\d+) completed\)", text)
        open_tasks = [(int(i), status, subject) for i, status, subject in found]
        return open_tasks, int(done.group(1)) if done else 0
    return None


def last_call(messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any], str] | None:
    """`(tool name, arguments, result text)` of the last message when it is a
    tool result."""
    plain = [m for m in messages if "Task ledger:" not in text_of(m)]
    if not plain or plain[-1].get("role") != "tool":
        return None
    result = plain[-1]
    for message in reversed(plain):
        for call in message.get("tool_calls") or []:
            if call["id"] == result.get("tool_call_id"):
                arguments = json.loads(call["function"]["arguments"] or "{}")
                return call["function"]["name"], arguments, text_of(result)
    return None


def decide(body: dict[str, Any]) -> tuple[str, Any]:
    """`("text", text)` or `("call", (name, arguments))`."""
    messages = body["messages"]
    # The summary request ends with the instruction, as a user message.
    final = messages[-1] if messages else {}
    if final.get("role") == "user" and SUMMARY_PROMPT in text_of(final):
        return "text", "Summary: the report is built in /report.md; the ledger has the rest."
    ledger = ledger_of(messages)
    if ledger is None:
        return "text", "No ledger was sent."
    open_tasks, done = ledger
    last = last_call(messages)
    if last is not None:
        name, arguments, result = last
        if name == "read_file":
            token = re.search(r"[A-Z]+-\d+", result)
            file = arguments["path"].rsplit("/", 1)[-1]
            section = f"## {file}\nKey token: {token.group(0) if token else '?'}\n"
            return "call", (
                "edit_file",
                {"path": "/report.md", "old_string": MARKER, "new_string": section + MARKER},
            )
        if name == "edit_file" and arguments["new_string"] == "":
            return "text", "Report complete."
        if name in ("edit_file", "write_file") and open_tasks:
            return "call", ("TaskUpdate", {"id": str(open_tasks[0][0]), "status": "completed"})
    total = len(open_tasks) + done
    if total < len(SUBJECTS):
        return "call", ("TaskCreate", {"subject": SUBJECTS[total], "description": "d"})
    if open_tasks:
        _, _, subject = open_tasks[0]
        if subject == SUBJECTS[0]:
            return "call", (
                "write_file",
                {"path": "/report.md", "content": f"# Report\n{MARKER}\n"},
            )
        return "call", ("read_file", {"path": "/in/" + subject.rsplit(" ", 1)[-1]})
    return "call", ("edit_file", {"path": "/report.md", "old_string": MARKER, "new_string": ""})


def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
    body = {
        "id": "chatcmpl-stub",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "stub-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body)}\n\n"


def stream_of(kind: str, what: Any, n: int) -> bytes:
    parts = [chunk({"role": "assistant", "content": ""})]
    if kind == "text":
        parts.append(chunk({"content": what}))
        finish = "stop"
    else:
        name, arguments = what
        call = {
            "index": 0,
            "id": f"call_{n}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
        parts.append(chunk({"tool_calls": [call]}))
        finish = "tool_calls"
    parts.append(chunk({}, finish))
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    final = {
        "id": "chatcmpl-stub",
        "object": "chat.completion.chunk",
        "choices": [],
        "usage": usage,
    }
    parts.append(f"data: {json.dumps(final)}\n\ndata: [DONE]\n\n")
    return "".join(parts).encode()


class ModelStub:
    """The server. `hold_at`: the number of the model request (summaries
    excluded) that is read but never answered, and `held` is set when it
    arrives, so the test can kill the agent's server while a call is in
    flight."""

    def __init__(self, hold_at: int | None = None) -> None:
        self.hold_at = hold_at
        self.held = asyncio.Event()
        self.requests = 0
        self.summaries = 0
        self._server: asyncio.Server | None = None
        self._open: set[asyncio.StreamWriter] = set()

    @property
    def base_url(self) -> str:
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/v1"

    async def __aenter__(self) -> ModelStub:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_: object) -> None:
        assert self._server is not None
        self._server.close()
        for writer in list(self._open):
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._open.add(writer)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            path = lines[0].split(" ")[1]
            length = 0
            for line in lines[1:]:
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1])
            raw = await reader.readexactly(length) if length else b""
            if path.endswith("/models"):
                reply = json.dumps({"data": [{"id": "stub-model", "max_model_len": 8000}]})
                self._send(writer, "application/json", reply.encode())
            else:
                await self._complete(writer, json.loads(raw))
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._open.discard(writer)
            writer.close()

    async def _complete(self, writer: asyncio.StreamWriter, body: dict[str, Any]) -> None:
        kind, what = decide(body)
        if kind == "text" and str(what).startswith("Summary:"):
            self.summaries += 1
        else:
            self.requests += 1
            if self.requests == self.hold_at:
                self.held.set()
                await asyncio.Event().wait()
        self._send(writer, "text/event-stream", stream_of(kind, what, self.requests))

    @staticmethod
    def _send(writer: asyncio.StreamWriter, content_type: str, body: bytes) -> None:
        head = (
            f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        )
        writer.write(head.encode() + body)
