"""Scripted agent configs for the tests, and the client's scripted turns."""

from __future__ import annotations

from typing import Any, cast

from linch_rust import AgentConfig
from linch_rust.scripted import MODEL, text_turn, tool_turn

__all__ = ["MODEL", "config", "text_turn", "tool_turn"]


def config(turns: list[dict[str, Any]], mode: str = "default", **extra: Any) -> AgentConfig:
    """A scripted agent config with no workspace, in permission mode `mode`."""
    fields: dict[str, Any] = {
        "namespace": "p12",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "permissions": {"mode": mode, "project_root": "/"},
        **extra,
    }
    return cast(AgentConfig, fields)
