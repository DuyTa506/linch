"""Loopback OpenAI-compatible chat server for case 14 (concurrent sessions).

    python chat_server.py [--delay S] [--workers K] [--stats FILE]

Prints `PORT <n>` once K processes listen on 127.0.0.1:<n> (`SO_REUSEPORT`).
Every POST gets one streamed Chat Completions answer after `--delay` s. The
number of `role: tool` messages picks the turn (Bash `echo s<i>`,
TaskCreate, TaskList, then text; `s<i>` is the prompt's session tag), so no
state is kept. On SIGTERM each process appends its overhead per request
(answer sent minus body read minus the delay, ms) to `--stats` as a JSON
line. Standard library only; it is the "network", so its CPU is not counted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sse_server import frame  # noqa: E402

TAG = re.compile(r"\bs\d+\b")
LENGTH = re.compile(rb"(?i)\r\ncontent-length:\s*(\d+)")
HEAD = b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncontent-length: %d\r\n\r\n"
MISSING = b"HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\n\r\n"
BASE = {"id": "chatcmpl-bench", "object": "chat.completion.chunk", "created": 1762400000}


def chunk(model: str, delta: dict, finish: str | None = None) -> str:
    choice = {"index": 0, "delta": delta, "finish_reason": finish}
    return frame({**BASE, "model": model, "choices": [choice]})


def answer(request: dict) -> bytes:
    """The stream for this request's turn."""
    messages = request.get("messages", [])
    turn = sum(m.get("role") == "tool" for m in messages)
    prompt = next((m.get("content") for m in messages if m.get("role") == "user"), "")
    if isinstance(prompt, list):
        prompt = " ".join(p.get("text", "") for p in prompt if isinstance(p, dict))
    found = TAG.search(prompt or "")
    tag = found.group(0) if found else "s?"
    model = request.get("model", "bench")
    calls = [
        ("Bash", {"command": f"echo {tag}"}),
        ("TaskCreate", {"subject": f"task {tag}", "description": "bench task"}),
        ("TaskList", {}),
    ]
    if turn < len(calls):
        name, args = calls[turn]
        function = {"name": name, "arguments": json.dumps(args)}
        call = {"index": 0, "id": f"call_{turn}", "type": "function", "function": function}
        out = [chunk(model, {"role": "assistant", "content": None, "tool_calls": [call]})]
        out.append(chunk(model, {}, "tool_calls"))
    else:
        out = [chunk(model, {"role": "assistant", "content": "done"}), chunk(model, {}, "stop")]
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    out.append(frame({**BASE, "model": model, "choices": [], "usage": usage}))
    out.append(frame("[DONE]"))
    return "".join(out).encode()


def listener(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("127.0.0.1", port))
    # The default backlog (100) drops SYNs in a burst of M connects.
    sock.listen(4096)
    sock.setblocking(False)
    return sock


async def serve(sock: socket.socket, delay: float, stats: str, children: list[int]) -> None:
    overhead: list[float] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = LENGTH.search(head)
                body = await reader.readexactly(int(length.group(1)) if length else 0)
                read = time.perf_counter()
                if not head.startswith(b"POST"):
                    writer.write(MISSING)
                    continue
                reply = answer(json.loads(body))
                # The kernel lets a long timeout run late by about 0.1-0.5%;
                # a short last sleep keeps the answer within a millisecond.
                await asyncio.sleep(max(0.0, delay - 0.01))
                await asyncio.sleep(max(0.0, read + delay - time.perf_counter()))
                writer.write(HEAD % len(reply) + reply)
                await writer.drain()
                overhead.append(1000 * (time.perf_counter() - read - delay))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    server = await asyncio.start_server(handle, sock=sock)
    await stop.wait()
    server.close()
    for pid in children:
        os.kill(pid, signal.SIGTERM)
    for pid in children:
        os.waitpid(pid, 0)
    if stats:
        with open(stats, "a") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "overhead_ms": overhead}) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=1.0, help="seconds before each answer")
    parser.add_argument("--workers", type=int, default=16, help="processes on the port")
    parser.add_argument("--stats", default="", help="file for each worker's overhead")
    args = parser.parse_args()
    # Bind every socket before forking, so all of them listen once PORT is out.
    first = listener(0)
    port = first.getsockname()[1]
    socks = [first] + [listener(port) for _ in range(args.workers - 1)]
    children = []
    for mine in socks[1:]:
        pid = os.fork()
        if pid == 0:
            for sock in socks:
                if sock is not mine:
                    sock.close()
            asyncio.run(serve(mine, args.delay, args.stats, []))
            os._exit(0)
        children.append(pid)
        mine.close()
    print(f"PORT {port}", flush=True)
    asyncio.run(serve(first, args.delay, args.stats, children))


if __name__ == "__main__":
    main()
