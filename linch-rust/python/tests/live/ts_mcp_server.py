"""A minimal stdio MCP server for the toolsec live tests (newline-delimited JSON-RPC).

Tools: `echo` (read-only), `add`, `fail` (answers `isError`), `poll` (answers "pending" a few times
then "ready"), `pid`. `MCP_PID_FILE` receives the pid; `MCP_STUBBORN` makes it ignore end of input
and SIGTERM so only a kill ends it.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from typing import Any

TOOLS = [
    {
        "name": "echo",
        "description": "Echo a text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "add",
        "description": "Add two integers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "fail",
        "description": "Always fails.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "poll",
        "description": "Report a job's state.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {"name": "pid", "description": "This server's pid.", "inputSchema": {"type": "object"}},
]
POLLS = {"n": 0}


def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "echo":
        text = str(args.get("text", ""))
    elif name == "add":
        text = str(int(args["a"]) + int(args["b"]))
    elif name == "fail":
        return {"content": [{"type": "text", "text": "deliberate failure"}], "isError": True}
    elif name == "poll":
        POLLS["n"] += 1
        text = "pending"
    elif name == "pid":
        text = str(os.getpid())
    else:
        return {"content": [{"type": "text", "text": f"no such tool {name}"}], "isError": True}
    return {"content": [{"type": "text", "text": text}]}


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    sys.stdout.flush()


def main() -> None:
    pid_file = os.environ.get("MCP_PID_FILE")
    if pid_file:
        with open(pid_file, "w") as handle:
            handle.write(str(os.getpid()))
    if os.environ.get("MCP_STUBBORN"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            continue
        if method == "initialize":
            version = message["params"].get("protocolVersion", "2025-03-26")
            send(
                {
                    "id": request_id,
                    "result": {
                        "protocolVersion": version,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "ts", "version": "1"},
                    },
                }
            )
        elif method == "tools/list":
            send({"id": request_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = message["params"]
            send({"id": request_id, "result": call(params["name"], params.get("arguments") or {})})
        elif method == "ping":
            send({"id": request_id, "result": {}})
        else:
            send({"id": request_id, "error": {"code": -32601, "message": f"unknown {method}"}})
    if os.environ.get("MCP_STUBBORN"):
        time.sleep(3600)


if __name__ == "__main__":
    main()
