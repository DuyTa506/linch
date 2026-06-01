"""The four virtual filesystem tools: ls, read_file, write_file, edit_file.

These tools operate on the virtual :class:`~agent_kit.filesystem.backend.FileBackend`
attached to the session, NOT on the real ``cwd`` filesystem.  They are the read-
back layer for large results that were automatically offloaded by
:mod:`~agent_kit.filesystem.offload`, and also give the agent an isolated scratch-
pad for notes, plans, and intermediate state.

Register them via::

    from agent_kit.filesystem import filesystem_tools
    registry = tools_from_defaults(extra=list(filesystem_tools()))

Or pass ``Agent(filesystem=StateFileBackend())`` which wires everything
automatically.
"""

from __future__ import annotations

from ..tools.base import ToolContext, ToolResult, ToolScope
from .backend import FileBackend, resolve_filesystem_backend


def _get_backend(ctx: ToolContext) -> FileBackend:
    fs = getattr(ctx, "filesystem", None)
    if fs is None:
        fs = resolve_filesystem_backend(ctx.deps)
    if fs is None:
        raise RuntimeError(
            "No filesystem backend attached. Pass Agent(filesystem=StateFileBackend()) "
            "or set ctx.deps.filesystem to a FileBackend."
        )
    return fs


class LsTool:
    """List files in the virtual filesystem."""

    name = "ls"
    description = (
        "List files in the virtual filesystem. "
        "Use after a tool offloads a large result to browse what is available, "
        "or to inspect your scratch notes. Pass a prefix to narrow the listing."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "prefix": {
                "type": "string",
                "description": "Optional path prefix to filter results, e.g. '/offload'.",
            }
        },
    }
    scope: ToolScope = "read"
    parallel_safe = True
    parallel = True

    def validate(self, raw: dict) -> dict:
        return {"prefix": str(raw.get("prefix", "") or "")}

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        backend = _get_backend(ctx)
        paths = await backend.ls(input.get("prefix", ""))
        if not paths:
            label = f" under '{input['prefix']}'" if input.get("prefix") else ""
            return ToolResult(
                content=f"No files found{label}.",
                summary="ls → 0 files",
            )
        content = "\n".join(paths)
        return ToolResult(
            content=content,
            summary=f"ls → {len(paths)} file{'s' if len(paths) != 1 else ''}",
        )

    def summarize(self, input: dict) -> str:
        prefix = input.get("prefix", "")
        return f"ls({prefix!r})" if prefix else "ls()"


class ReadFileTool:
    """Read a file from the virtual filesystem, with optional line windowing."""

    name = "read_file"
    description = (
        "Read a file from the virtual filesystem. Supports paged reading via "
        "offset (1-indexed line number) and limit. Use this to inspect offloaded "
        "tool results or files you wrote with write_file."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Virtual file path, e.g. '/offload/web_search_abc.txt'.",
            },
            "offset": {
                "type": "integer",
                "description": "1-indexed line to start from (default 1).",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum lines to return (default: entire file).",
            },
        },
        "required": ["path"],
    }
    scope: ToolScope = "read"
    parallel_safe = True
    parallel = True

    def validate(self, raw: dict) -> dict:
        path = raw.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        result: dict = {"path": path.strip()}
        for key in ("offset", "limit"):
            v = raw.get(key)
            if v is not None:
                result[key] = int(v)
        return result

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        backend = _get_backend(ctx)
        path = input["path"]
        offset = int(input.get("offset") or 0)
        limit = input.get("limit")
        limit_i = int(limit) if limit is not None else None
        try:
            text = await backend.read(path, offset=offset, limit=limit_i)
        except FileNotFoundError:
            return ToolResult(
                content=f"File not found: '{path}'. Use ls() to see available files.",
                summary=f"read_file({path!r}) → not found",
                is_error=True,
            )
        return ToolResult(
            content=text,
            summary=f"read_file({path!r})",
        )

    def summarize(self, input: dict) -> str:
        path = input.get("path", "?")
        offset = input.get("offset")
        limit = input.get("limit")
        parts = []
        if offset:
            parts.append(f"offset={offset}")
        if limit:
            parts.append(f"limit={limit}")
        suffix = f", {', '.join(parts)}" if parts else ""
        return f"read_file({path!r}{suffix})"


class WriteFileTool:
    """Write a new file to the virtual filesystem."""

    name = "write_file"
    description = (
        "Write content to a virtual file. Creates the file if it does not exist, "
        "or overwrites it entirely. Use as a scratchpad for notes, plans, and "
        "intermediate results that you want to reference across turns."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Virtual file path to write."},
            "content": {"type": "string", "description": "File content."},
        },
        "required": ["path", "content"],
    }
    scope: ToolScope = "write"
    parallel_safe = False
    parallel = False

    def validate(self, raw: dict) -> dict:
        path = raw.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        content = raw.get("content")
        if content is None:
            raise ValueError("content is required")
        return {"path": path.strip(), "content": str(content)}

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        backend = _get_backend(ctx)
        path = input["path"]
        content = input["content"]
        await backend.write(path, content)
        lines = content.count("\n") + 1
        return ToolResult(
            content=f"Written {lines} line{'s' if lines != 1 else ''} to '{path}'.",
            summary=f"write_file({path!r})",
        )

    def summarize(self, input: dict) -> str:
        return f"write_file({input.get('path', '?')!r})"


class EditFileTool:
    """Edit an existing file in the virtual filesystem."""

    name = "edit_file"
    description = (
        "Replace an exact string in a virtual file. The old_string must match "
        "the file contents exactly (including indentation). Use replace_all to "
        "substitute every occurrence. Returns the replacement count."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Virtual file path."},
            "old_string": {"type": "string", "description": "Exact text to find and replace."},
            "new_string": {"type": "string", "description": "Replacement text."},
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence (default false: error if not unique).",
            },
        },
        "required": ["path", "old_string", "new_string"],
    }
    scope: ToolScope = "write"
    parallel_safe = False
    parallel = False

    def validate(self, raw: dict) -> dict:
        path = raw.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        old = raw.get("old_string")
        if not isinstance(old, str):
            raise ValueError("old_string must be a string")
        new = raw.get("new_string")
        if not isinstance(new, str):
            raise ValueError("new_string must be a string")
        return {
            "path": path.strip(),
            "old_string": old,
            "new_string": new,
            "replace_all": bool(raw.get("replace_all", False)),
        }

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        backend = _get_backend(ctx)
        path = input["path"]
        try:
            count = await backend.edit(
                path,
                input["old_string"],
                input["new_string"],
                replace_all=bool(input.get("replace_all", False)),
            )
        except FileNotFoundError:
            return ToolResult(
                content=f"File not found: '{path}'. Use ls() to see available files.",
                summary=f"edit_file({path!r}) → not found",
                is_error=True,
            )
        except ValueError as exc:
            return ToolResult(
                content=str(exc),
                summary=f"edit_file({path!r}) → error",
                is_error=True,
            )
        return ToolResult(
            content=f"Made {count} replacement{'s' if count != 1 else ''} in '{path}'.",
            summary=f"edit_file({path!r})",
        )

    def summarize(self, input: dict) -> str:
        return f"edit_file({input.get('path', '?')!r})"


def filesystem_tools(
    *,
    ls: bool = True,
    read_file: bool = True,
    write_file: bool = True,
    edit_file: bool = True,
) -> list:
    """Return a list of filesystem tool instances, optionally filtered."""
    out = []
    if ls:
        out.append(LsTool())
    if read_file:
        out.append(ReadFileTool())
    if write_file:
        out.append(WriteFileTool())
    if edit_file:
        out.append(EditFileTool())
    return out
