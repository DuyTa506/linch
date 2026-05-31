from __future__ import annotations

import re

_BAD = re.compile(r"[^a-zA-Z0-9_-]")


def normalize_name_for_mcp(name: str) -> str:
    return _BAD.sub("_", name)


def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    return f"mcp__{normalize_name_for_mcp(server_name)}__{normalize_name_for_mcp(tool_name)}"
