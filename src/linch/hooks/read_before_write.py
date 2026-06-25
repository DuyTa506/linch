from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..tools.builtin import resolve_under
from .contexts import PostToolUseContext, PreToolUseContext
from .types import HookResult


@dataclass(frozen=True, slots=True)
class ReadBeforeWriteConfig:
    """Configuration for :class:`ReadBeforeWriteHook`."""

    read_tools: dict[str, str] = field(
        default_factory=lambda: {
            "Read": "workspace",
            "read_file": "virtual",
        }
    )
    edit_tools: dict[str, str] = field(
        default_factory=lambda: {
            "Edit": "workspace",
            "edit_file": "virtual",
        }
    )
    error_message: str = "Error: You must read this file before editing it."


class ReadBeforeWriteHook:
    """Block edit-in-place tools until the target file has been read."""

    name = "read_before_write"

    def __init__(self, config: ReadBeforeWriteConfig | None = None) -> None:
        self.config = config or ReadBeforeWriteConfig()

    async def on_pre_tool_use(self, ctx: PreToolUseContext) -> HookResult | None:
        kind = self.config.edit_tools.get(ctx.tool_name)
        if kind is None:
            return None
        key = _key_for_input(kind, ctx.input, ctx)
        if key is None:
            return None
        tracker = getattr(ctx.session, "file_read_tracker", None)
        if tracker is not None and not tracker.has_read(key):
            return HookResult.block(self.config.error_message)
        return None

    async def on_post_tool_use(self, ctx: PostToolUseContext) -> HookResult | None:
        kind = self.config.read_tools.get(ctx.tool_name)
        if kind is None:
            return None
        result = ctx.result
        if result is None or getattr(result, "is_error", False):
            return None
        key = _key_for_input(kind, ctx.input, ctx)
        if key is None:
            return None
        tracker = getattr(ctx.session, "file_read_tracker", None)
        if tracker is not None:
            tracker.mark_read(key)
        return None


def _key_for_input(kind: str, input: dict[str, Any], ctx: Any) -> str | None:
    if kind == "workspace":
        raw = input.get("file_path")
        if raw is None:
            raw = input.get("path")
        if not isinstance(raw, str) or raw == "":
            return None
        try:
            return str(resolve_under(_cwd(ctx.session), raw))
        except Exception:
            return None
    if kind == "virtual":
        raw = input.get("path")
        if not isinstance(raw, str) or not raw.strip():
            return None
        from ..filesystem.backend import normalize_path

        return f"vfs:{normalize_path(raw.strip())}"
    return None


def _cwd(session: Any) -> str:
    override = getattr(session, "cwd_override", None)
    if isinstance(override, str) and override:
        return override
    agent = getattr(session, "agent", None)
    cwd = getattr(agent, "cwd", None)
    if isinstance(cwd, str) and cwd:
        return cwd
    return str(Path.cwd())
