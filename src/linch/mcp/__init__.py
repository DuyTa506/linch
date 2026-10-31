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
        """Represent an unavailable optional MCP connection."""

        def __init__(self, tools: list[object] | None = None) -> None:
            """Create an empty placeholder connection.

            Args:
                tools: Optional tool list retained for API shape compatibility.
            """
            self.tools = tools or []

        async def close(self) -> None:
            """Close the placeholder connection without side effects."""
            return None

    async def connect_mcp_servers(*_args: object, **_kwargs: object) -> McpConnection:
        """Report that the optional MCP dependency is unavailable.

        Args:
            _args: Positional connection arguments retained for API compatibility.
            _kwargs: Keyword connection arguments retained for API compatibility.

        Raises:
            ModuleNotFoundError: Always, when the MCP extra is not installed.
        """
        raise ModuleNotFoundError(
            "MCP support requires the optional 'mcp' dependency. "
            "Install with: pip install 'linch[mcp]'"
        )

    def make_mcp_tool(*_args: object, **_kwargs: object) -> object:
        """Report that the optional MCP dependency is unavailable.

        Args:
            _args: Positional tool arguments retained for API compatibility.
            _kwargs: Keyword tool arguments retained for API compatibility.

        Raises:
            ModuleNotFoundError: Always, when the MCP extra is not installed.
        """
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
