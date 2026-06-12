from __future__ import annotations

from .config import (
    McpHttpServerConfig,
    McpServerConfig,
    McpStdioServerConfig,
    mcp_server_type,
)
from .naming import build_mcp_tool_name, normalize_name_for_mcp
from .permission_bridge import mcp_permission_rules

try:
    from .client import McpConnection, connect_mcp_servers  # type: ignore[assignment]
    from .tool import make_mcp_tool  # type: ignore[assignment]
except ModuleNotFoundError as exc:
    missing_name = getattr(exc, "name", None)
    if missing_name not in {None, "mcp"} and not str(missing_name).startswith("mcp."):
        raise

    class McpConnection:  # type: ignore[no-redef]
        def __init__(self, tools: list[object] | None = None) -> None:
            self.tools = tools or []

        async def close(self) -> None:
            return None

    async def connect_mcp_servers(*_args: object, **_kwargs: object) -> McpConnection:
        raise ModuleNotFoundError(
            "MCP support requires the optional 'mcp' dependency. "
            "Install with: pip install 'linch[mcp]'"
        )

    def make_mcp_tool(*_args: object, **_kwargs: object) -> object:
        raise ModuleNotFoundError(
            "MCP support requires the optional 'mcp' dependency. "
            "Install with: pip install 'linch[mcp]'"
        )


__all__ = [
    "McpConnection",
    "McpHttpServerConfig",
    "McpServerConfig",
    "McpStdioServerConfig",
    "build_mcp_tool_name",
    "connect_mcp_servers",
    "make_mcp_tool",
    "mcp_permission_rules",
    "mcp_server_type",
    "normalize_name_for_mcp",
]
