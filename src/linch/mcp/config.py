from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(slots=True)
class McpStdioServerConfig:
    type: Literal["stdio"] | None = None
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None


@dataclass(slots=True)
class McpHttpServerConfig:
    type: Literal["http"] = "http"
    url: str = ""
    headers: dict[str, str] | None = None


McpServerConfig = McpStdioServerConfig | McpHttpServerConfig


def mcp_server_type(config: McpServerConfig) -> str:
    t: object = getattr(config, "type", None)
    if t == "http":
        return "http"
    return "stdio"
