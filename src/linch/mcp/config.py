from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(slots=True)
class McpStdioServerConfig:
    """Configure an MCP server launched through a stdio subprocess.

    Args:
        type: Optional explicit transport marker; ``"stdio"`` is the default path.
        command: Executable used to launch the MCP server.
        args: Command-line arguments passed to the executable.
        env: Optional environment mapping supplied to the subprocess.
        timeout_s: Per-tool-call SDK request read timeout in seconds (default 300).

    Attributes:
        type: Optional stdio transport marker.
        command: Server executable.
        args: Server command-line arguments.
        env: Optional subprocess environment.
        timeout_s: Per-tool-call request read timeout in seconds.
    """

    type: Literal["stdio"] | None = None
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    timeout_s: float = 300


@dataclass(slots=True)
class McpHttpServerConfig:
    """Configure an MCP server reached through streamable HTTP.

    Args:
        type: HTTP transport marker, fixed to ``"http"``.
        url: Streamable HTTP endpoint for the MCP server.
        headers: Optional HTTP headers sent with the connection.
        timeout_s: Per-tool-call SDK request read timeout in seconds (default 300).

    Attributes:
        type: The HTTP transport marker.
        url: Streamable HTTP endpoint.
        headers: Optional connection headers.
        timeout_s: Per-tool-call request read timeout in seconds.
    """

    type: Literal["http"] = "http"
    url: str = ""
    headers: dict[str, str] | None = None
    timeout_s: float = 300


McpServerConfig = McpStdioServerConfig | McpHttpServerConfig
"""Supported stdio and HTTP MCP server configuration variants."""


def mcp_server_type(config: McpServerConfig) -> str:
    """Return the transport kind selected by an MCP server configuration.

    Args:
        config: Stdio or HTTP server configuration to classify.

    Returns:
        ``"http"`` for an HTTP configuration and ``"stdio"`` otherwise.
    """
    t: object = getattr(config, "type", None)
    if t == "http":
        return "http"
    return "stdio"
