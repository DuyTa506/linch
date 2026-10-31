from __future__ import annotations

import re

_BAD = re.compile(r"[^a-zA-Z0-9_-]")


def normalize_name_for_mcp(name: str) -> str:
    """Replace characters unsupported by MCP tool names with underscores.

    Args:
        name: Server or tool name to normalize.

    Returns:
        The name containing only ASCII letters, digits, underscores, and hyphens.
    """
    return _BAD.sub("_", name)


def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Build the stable Linch name for one server-provided MCP tool.

    Args:
        server_name: Configured MCP server name.
        tool_name: Name declared by the MCP server.

    Returns:
        A normalized ``mcp__server__tool`` name suitable for a Linch registry.
    """
    return f"mcp__{normalize_name_for_mcp(server_name)}__{normalize_name_for_mcp(tool_name)}"
