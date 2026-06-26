from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..tools.builtin import resolve_under
from .contexts import PostToolUseContext, PreToolUseContext
from .types import HookResult

logger = logging.getLogger(__name__)


class _PathResolveError(Exception):
    """A path was supplied but could not be resolved (e.g. escapes the cwd)."""


@dataclass(frozen=True, slots=True)
class ReadBeforeWriteConfig:
    """Configuration for :class:`ReadBeforeWriteHook`.

    Scope note: this hook governs the **virtual filesystem** edit gate and
    whole-file **overwrites** of already-existing files. Workspace ``Edit`` is
    gated by the builtin ``Edit`` tool itself, which keeps its own
    "You must Read this file before editing it." message — the hook deliberately
    does not duplicate that gate, so there is a single source of truth and a
    single error string for workspace edits. ``read_before_write=False`` disables
    this hook's gates; the builtin ``Edit`` read requirement is intrinsic to that
    tool and is not affected by the flag.
    """

    #: Tools whose successful, full-file result means "the target is now known"
    #: → mark it read. ``Read`` is included because a *cached* Read short-circuits
    #: the builtin tool's own ``mark_read`` (execute is skipped on a cache hit),
    #: so the post-hook is what records it. Writing a whole file also counts as
    #: knowing its content.
    read_tools: dict[str, str] = field(
        default_factory=lambda: {
            "Read": "workspace",
            "read_file": "virtual",
            "write_file": "virtual",
            "Write": "workspace",
        }
    )
    #: Edit-in-place tools blocked until the target has been read. Workspace
    #: ``Edit`` is intentionally absent — the builtin tool gates it.
    edit_tools: dict[str, str] = field(
        default_factory=lambda: {
            "edit_file": "virtual",
        }
    )
    #: Whole-file overwrite tools blocked only when the target **already exists**
    #: and has not been read. Empty by default: overwriting a whole file is the
    #: documented purpose of Write/write_file (regenerating output, scratchpad
    #: progress files), so gating it by default would break legitimate flows.
    #: Opt in for safety-conscious hosts, e.g.
    #: ``ReadBeforeWriteConfig(overwrite_tools={"Write": "workspace"})``. After a
    #: successful write the file is marked read (see ``read_tools``), so the
    #: tracker stays honest and a later Edit is allowed.
    overwrite_tools: dict[str, str] = field(default_factory=dict)
    edit_error_message: str = "Error: You must read this file before editing it."
    overwrite_error_message: str = (
        "Error: You must read this file before overwriting it; it already exists."
    )


class ReadBeforeWriteHook:
    """Block edit-in-place and blind-overwrite tools until the file has been read."""

    name = "read_before_write"

    def __init__(self, config: ReadBeforeWriteConfig | None = None) -> None:
        self.config = config or ReadBeforeWriteConfig()

    async def on_pre_tool_use(self, ctx: PreToolUseContext) -> HookResult | None:
        # Edit-in-place: always requires a prior read.
        kind = self.config.edit_tools.get(ctx.tool_name)
        if kind is not None:
            return self._gate_unread(kind, ctx, self.config.edit_error_message)

        # Whole-file overwrite: only gated when the target already exists, so
        # creating a new file is never blocked.
        kind = self.config.overwrite_tools.get(ctx.tool_name)
        if kind is not None and await self._target_exists(kind, ctx):
            return self._gate_unread(kind, ctx, self.config.overwrite_error_message)
        return None

    async def on_post_tool_use(self, ctx: PostToolUseContext) -> HookResult | None:
        kind = self.config.read_tools.get(ctx.tool_name)
        if kind is None:
            return None
        result = ctx.result
        if result is None or getattr(result, "is_error", False):
            return None
        # A windowed (offset/limit) read only saw part of the file, so it must
        # not grant edit permission over the regions that were never seen.
        if _is_windowed_read(ctx.tool_name, ctx.input):
            return None
        try:
            key = _key_for_input(kind, ctx.input, ctx)
        except _PathResolveError:
            logger.debug(
                "read_before_write: could not resolve path to mark read for %s",
                ctx.tool_name,
            )
            return None
        if key is None:
            return None
        tracker = getattr(ctx.session, "file_read_tracker", None)
        if tracker is not None:
            tracker.mark_read(key)
        return None

    def _gate_unread(self, kind: str, ctx: Any, message: str) -> HookResult | None:
        try:
            key = _key_for_input(kind, ctx.input, ctx)
        except _PathResolveError:
            # An unresolvable path on a write gate fails CLOSED (block) rather
            # than silently allowing the write — the path may be escaping cwd.
            logger.warning(
                "read_before_write: could not resolve path for %s; blocking (fail-closed)",
                ctx.tool_name,
            )
            return HookResult.block(message)
        if key is None:
            return None
        tracker = getattr(ctx.session, "file_read_tracker", None)
        if tracker is not None and not tracker.has_read(key):
            return HookResult.block(message)
        return None

    async def _target_exists(self, kind: str, ctx: Any) -> bool:
        if kind == "workspace":
            try:
                key = _key_for_input(kind, ctx.input, ctx)
            except _PathResolveError:
                return True  # can't resolve → treat as existing so the gate fails closed
            return key is not None and Path(key).exists()
        if kind == "virtual":
            raw = _virtual_raw(ctx.input)
            if raw is None:
                return False
            backend = getattr(ctx.session, "filesystem", None)
            if backend is None:
                return False
            from ..filesystem.backend import normalize_path

            try:
                return await backend.exists(normalize_path(raw))
            except Exception:
                # Fail CLOSED, like the workspace branch: if we can't tell whether
                # the file exists, treat it as existing so the overwrite gate
                # blocks rather than risking a blind overwrite.
                logger.warning(
                    "read_before_write: backend.exists failed for %r; gating (fail-closed)",
                    raw,
                )
                return True
        return False


def _is_windowed_read(tool_name: str, input: dict[str, Any]) -> bool:
    if tool_name not in ("Read", "read_file"):
        return False
    offset = input.get("offset")
    limit = input.get("limit")
    return limit is not None or (isinstance(offset, int) and offset > 1)


def _workspace_raw(input: dict[str, Any]) -> str | None:
    raw = input.get("file_path")
    if raw is None:
        raw = input.get("path")
    return raw if isinstance(raw, str) and raw != "" else None


def _virtual_raw(input: dict[str, Any]) -> str | None:
    raw = input.get("path")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _key_for_input(kind: str, input: dict[str, Any], ctx: Any) -> str | None:
    if kind == "workspace":
        raw = _workspace_raw(input)
        if raw is None:
            return None
        try:
            return str(resolve_under(_cwd(ctx.session), raw))
        except Exception as exc:  # path escapes cwd / cannot be resolved
            raise _PathResolveError(str(exc)) from exc
    if kind == "virtual":
        raw = _virtual_raw(input)
        if raw is None:
            return None
        from ..filesystem.backend import normalize_path

        return f"vfs:{normalize_path(raw)}"
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
