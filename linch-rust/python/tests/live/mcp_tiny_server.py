"""A minimal stdio MCP server for the live tests: newline-delimited JSON-RPC.

Tools: `add` (a, b -> a + b) and `shout` (text -> upper case). It writes its
pid to the file named by `MCP_PID_FILE`, so a test can check it is gone after
the agent closes. With `MCP_STUBBORN` set it ignores end of input and SIGTERM, so
only a kill ends it.
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
        "name": "add",
        "description": "Add two integers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "shout",
        "description": "Upper-case a text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
]


def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "add":
        text = str(int(args["a"]) + int(args["b"]))
    elif name == "shout":
        text = str(args["text"]).upper()
    else:
        return {"content": [{"type": "text", "text": f"no such tool {name}"}], "isError": True}
    return {"content": [{"type": "text", "text": text}]}


def reply(request_id: Any, result: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()


def main() -> None:
    pid_file = os.environ.get("MCP_PID_FILE")
    if pid_file:
        with open(pid_file, "w") as handle:
            handle.write(str(os.getpid()))
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            continue  # a notification
        if method == "initialize":
            reply(
                request_id,
                {
                    "protocolVersion": message["params"].get("protocolVersion", "2025-03-26"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "tiny", "version": "1"},
                },
            )
        elif method == "tools/list":
            reply(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = message["params"]
            reply(request_id, call(params["name"], params.get("arguments") or {}))
        elif method == "ping":
            reply(request_id, {})
        else:
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": f"unknown method {method}"},
                    }
                )
                + "\n"
            )
            sys.stdout.flush()


if __name__ == "__main__":
    main()
    if os.environ.get("MCP_STUBBORN"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(3600)
