from __future__ import annotations

import asyncio
import glob
import os
import re
import shutil
from pathlib import Path
from typing import Any, cast

from linch._blocking import run_blocking
from linch.errors import ToolExecutionError
from linch.filesystem.disk import _atomic_write

from ._limits import OUTPUT_CAP
from .base import ResourceAccess, ToolContext, ToolResult, ToolScope, require_str


def resolve_under(cwd: str, path_str: str) -> Path:
    """Resolve a path while enforcing containment under the working directory.

    Args:
        cwd: Allowed working-directory root.
        path_str: Relative or absolute path to resolve.

    Returns:
        Resolved path below ``cwd``.

    Raises:
        ToolExecutionError: If the path escapes ``cwd``.
    """

    base = Path(cwd).resolve()
    target = (
        (base / path_str).resolve()
        if not Path(path_str).is_absolute()
        else Path(path_str).resolve()
    )
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ToolExecutionError(f"path escapes cwd: {path_str}") from exc
    return target


def _file_stamp(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_mtime_ns, info.st_size


async def _run_mutation(tool: Any, input: dict[str, object], ctx: ToolContext) -> ToolResult:
    task = asyncio.create_task(run_blocking(tool._execute, input, ctx))
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except BaseException:
            if cancelled is None:
                raise
    if cancelled is not None:
        if not task.cancelled():
            task.exception()
        raise cancelled
    return task.result()


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


class ReadTool:
    """Read UTF-8 text from a workspace file."""

    name = "Read"
    description = "Read a UTF-8 text file from the workspace."
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "offset": {"type": "integer", "description": "Line number to start from (1-indexed)"},
            "limit": {"type": "integer", "description": "Max lines to return"},
        },
        "required": ["file_path"],
    }
    scope: ToolScope = "read"
    parallel: bool = True

    def __init__(self, *, max_read_bytes: int = 10 * 1024 * 1024) -> None:
        if type(max_read_bytes) is not int or max_read_bytes <= 0:
            raise ValueError("max_read_bytes must be a positive integer")
        self.max_read_bytes = max_read_bytes

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        """Validate a read path and optional line window.

        Args:
            raw: Provider-supplied read request.

        Returns:
            Normalized read request.

        Raises:
            ValueError: If the path is missing or empty.
        """

        file_path = raw.get("file_path")
        if not isinstance(file_path, str) or file_path == "":
            raise ValueError("file_path must be a non-empty string")
        result: dict[str, object] = {"file_path": file_path}
        for key in ("offset", "limit"):
            v = raw.get(key)
            if v is not None:
                result[key] = _to_int(v, 0)
        return result

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        result, (path, stamp, bounds) = await run_blocking(self._execute, input, ctx)
        if ctx.file_read_tracker is not None and bounds is not None:
            ctx.file_read_tracker.set_stamp(path, stamp, reset=True)
            ctx.file_read_tracker.mark_read(path, *bounds)
        return result

    def _execute(self, input: dict[str, object], ctx: ToolContext) -> tuple[ToolResult, Any]:
        """Read and number selected lines from a workspace file.

        Args:
            input: Validated read request.
            ctx: Runtime context containing the workspace root.

        Returns:
            Tool result containing numbered file text.

        Raises:
            ToolExecutionError: If the path cannot be resolved or read.
        """

        path = resolve_under(ctx.cwd, str(input["file_path"]))
        try:
            if path.stat().st_size > self.max_read_bytes:
                raise ToolExecutionError("File exceeds max_read_bytes; use Grep to narrow results.")
            with path.open("rb") as file:
                info = os.fstat(file.fileno())
                stamp = (info.st_mtime_ns, info.st_size)
                data = file.read(self.max_read_bytes + 1)
            if len(data) > self.max_read_bytes:
                raise ToolExecutionError("File exceeds max_read_bytes; use Grep to narrow results.")
            if _file_stamp(path) != stamp:
                raise ToolExecutionError("File changed while reading; Read this file again.")
            if _is_binary(data):
                raise ToolExecutionError("Binary files cannot be read as text.")
            text = data.decode("utf-8")
        except Exception as exc:
            raise ToolExecutionError(str(exc)) from exc

        lines = text.split("\n")
        offset = _to_int(input.get("offset"), 1)
        offset = offset if offset > 0 else 1
        limit = _to_int(input.get("limit"), 2000) or 2000
        start_index = min(offset - 1, len(lines))
        end_index = min(start_index + limit, len(lines)) if limit > 0 else len(lines)
        full = start_index == 0 and end_index == len(lines)
        bounds: tuple[int, ...] | None = () if full else (start_index + 1, end_index)
        if start_index >= end_index:
            bounds = None
        lines = lines[start_index:end_index]
        numbered = [f"{idx + offset}\t{line}" for idx, line in enumerate(lines)]
        text = "\n".join(numbered) if numbered else "<file is empty>"
        return ToolResult(content=text, summary=f"Read {path}"), (str(path), stamp, bounds)

    def summarize(self, input: dict[str, object]) -> str:
        """Summarize a read call.

        Args:
            input: Validated read request.

        Returns:
            Human-readable summary.
        """

        return f"Read {input['file_path']}"

    def resources(self, input: dict[str, object]) -> list[ResourceAccess]:
        """Describe the file resource read by the call.

        Args:
            input: Validated read request.

        Returns:
            One read resource descriptor.
        """

        return [ResourceAccess(resource=f"file:{input['file_path']}", mode="read")]


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


class WriteTool:
    """Write UTF-8 text to a workspace file."""

    name = "Write"
    description = "Write UTF-8 text to a workspace file, creating parent directories."
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["file_path", "content"],
    }
    scope: ToolScope = "write"
    parallel: bool = False

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        """Validate a write path and text content.

        Args:
            raw: Provider-supplied write request.

        Returns:
            Normalized write request.

        Raises:
            ValueError: If the path or content is invalid.
        """

        file_path = raw.get("file_path")
        if not isinstance(file_path, str) or file_path == "":
            raise ValueError("file_path must be a non-empty string")
        content = raw.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        return {
            "file_path": file_path,
            "content": content,
        }

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        return await _run_mutation(self, input, ctx)

    def _execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        """Write content to a workspace file, creating parent directories.

        Args:
            input: Validated write request.
            ctx: Runtime workspace context.

        Returns:
            Tool result describing the written path.

        Raises:
            ToolExecutionError: If the path escapes the workspace.
        """

        path = resolve_under(ctx.cwd, str(input["file_path"]))
        if path.exists() and ctx.file_read_tracker is not None:
            if not ctx.file_read_tracker.has_read(
                str(path)
            ) or not ctx.file_read_tracker.is_current(str(path), _file_stamp(path)):
                return ToolResult(content="Read this file again before writing it.", is_error=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, str(input["content"]))
        if ctx.file_read_tracker is not None:
            ctx.file_read_tracker.mark_read(str(path))
            ctx.file_read_tracker.set_stamp(str(path), _file_stamp(path))
        return ToolResult(content=f"Wrote {path}", summary=f"Wrote {path}")

    def summarize(self, input: dict[str, object]) -> str:
        """Summarize a write call.

        Args:
            input: Validated write request.

        Returns:
            Human-readable summary.
        """

        return f"Write {input['file_path']}"

    def resources(self, input: dict[str, object]) -> list[ResourceAccess]:
        """Describe the file resource written by the call.

        Args:
            input: Validated write request.

        Returns:
            One write resource descriptor.
        """

        return [ResourceAccess(resource=f"file:{input['file_path']}", mode="write")]


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


class EditTool:
    """Replace exact text in a workspace file."""

    name = "Edit"
    description = "Replace text in a workspace file with exact byte-for-byte match."
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        "required": ["file_path", "old_string", "new_string"],
    }
    scope: ToolScope = "write"
    parallel: bool = False

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        """Validate an exact replacement request.

        Args:
            raw: Provider-supplied edit request.

        Returns:
            Normalized edit request.

        Raises:
            ValueError: If path or replacement strings are invalid.
        """

        file_path = raw.get("file_path")
        old_string = raw.get("old_string")
        new_string = raw.get("new_string")
        if not isinstance(file_path, str) or file_path == "":
            raise ValueError("file_path must be a non-empty string")
        if not isinstance(old_string, str):
            raise ValueError("old_string must be a string")
        if not isinstance(new_string, str):
            raise ValueError("new_string must be a string")
        return {
            "file_path": file_path,
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": bool(raw.get("replace_all", False)),
        }

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        return await _run_mutation(self, input, ctx)

    def _execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        """Apply an exact replacement after read-before-write checks.

        Args:
            input: Validated edit request.
            ctx: Runtime workspace context.

        Returns:
            Tool result describing success or a policy error.

        Raises:
            ToolExecutionError: If the path escapes the workspace or old text is absent.
        """

        path = resolve_under(ctx.cwd, str(input["file_path"]))
        if ctx.file_read_tracker is not None and (
            not path.exists() or not ctx.file_read_tracker.is_current(str(path), _file_stamp(path))
        ):
            return ToolResult(content="Read this file again before editing it.", is_error=True)
        text = path.read_bytes().decode("utf-8")
        old = str(input["old_string"])
        replace_all = bool(input.get("replace_all", False))
        match_starts: list[int] = []
        if old == "":
            match_starts.append(0)
        else:
            cursor = 0
            while (match_start := text.find(old, cursor)) >= 0:
                match_starts.append(match_start)
                cursor = match_start + len(old)
        if ctx.file_read_tracker is not None:
            if not match_starts:
                if not ctx.file_read_tracker.has_read(str(path)):
                    return ToolResult(
                        content="Error: You must Read this file before editing it.",
                        summary=self.summarize(input),
                        is_error=True,
                    )
            elif old == "" and replace_all:
                fully_read = ctx.file_read_tracker.has_read_range(
                    str(path), 1, len(text.split("\n"))
                )
                if not fully_read:
                    return ToolResult(
                        content="Error: You must Read this file before editing it.",
                        summary=self.summarize(input),
                        is_error=True,
                    )
            else:
                checks = match_starts if replace_all else match_starts[:1]
                for match_start in checks:
                    start_line = text.count("\n", 0, match_start) + 1
                    end_line = start_line + old.count("\n") - int(old.endswith("\n"))
                    if not ctx.file_read_tracker.has_read_range(str(path), start_line, end_line):
                        return ToolResult(
                            content="Error: You must Read this file before editing it.",
                            summary=self.summarize(input),
                            is_error=True,
                        )
        if old not in text:
            raise ToolExecutionError("old text not found")
        if not replace_all and text.count(old) > 1:
            return ToolResult(
                content=(
                    "Error: old_string appears multiple times. Provide a unique match "
                    "or set replace_all=true."
                ),
                summary=self.summarize(input),
                is_error=True,
            )
        if replace_all:
            new_text = text.replace(old, str(input["new_string"]))
        else:
            new_text = text.replace(old, str(input["new_string"]), 1)
        _atomic_write(path, new_text)
        if ctx.file_read_tracker is not None:
            ctx.file_read_tracker.invalidate_ranges(str(path))
            ctx.file_read_tracker.set_stamp(str(path), _file_stamp(path))
        return ToolResult(content=f"Edited {path}", summary=f"Edited {path}")

    def summarize(self, input: dict[str, object]) -> str:
        """Summarize an edit call.

        Args:
            input: Validated edit request.

        Returns:
            Human-readable summary.
        """

        return f"Edit {input['file_path']}"

    def resources(self, input: dict[str, object]) -> list[ResourceAccess]:
        """Describe the file resource edited by the call.

        Args:
            input: Validated edit request.

        Returns:
            One write resource descriptor.
        """

        return [ResourceAccess(resource=f"file:{input['file_path']}", mode="write")]


# ---------------------------------------------------------------------------
# Bash
# ---------------------------------------------------------------------------


class BashTool:
    """Run a shell command through the configured execution backend."""

    name = "Bash"
    description = (
        "Run shell commands. timeout_ms returns without killing; follow with BashProcess. "
        "Processes stop when the run ends. Do not pipe long commands through tail. Default 120s."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "run_in_background": {"type": "boolean"},
            "timeout_ms": {
                "type": "integer",
                "description": "Return after this many ms. Max 1800000 (30 min).",
            },
        },
        "required": ["command"],
    }
    scope: ToolScope = "exec"
    parallel: bool = False

    def __init__(
        self, *, execution: Any = None, max_processes: int = 4, max_runtime_ms: int = 1800000
    ) -> None:
        """Create a shell tool using an optional execution capability.

        Args:
            execution: Optional execution capability; defaults to local execution.
        """

        from .execution import LocalBackend

        if max_processes < 1 or max_runtime_ms < 1:
            raise ValueError("process limits must be positive")
        self.max_processes, self.max_runtime_ms = max_processes, max_runtime_ms
        # An ExecutionBackend (capability seam) sources the shell half.
        self._backend: Any = execution.shell if execution is not None else LocalBackend()

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        """Validate a shell command and clamp its timeout.

        Args:
            raw: Provider-supplied command request.

        Returns:
            Normalized command and timeout.

        Raises:
            ValueError: If the command is missing or empty.
        """

        timeout = _to_int(raw.get("timeout_ms", 120000), 120000)
        if timeout <= 0:
            timeout = 120000
        return {
            "command": require_str(raw, "command"),
            "timeout_ms": min(timeout, 1800000),
            "run_in_background": bool(raw.get("run_in_background", False)),
        }

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        """Run a command in the workspace.

        Args:
            input: Validated command request.
            ctx: Runtime context containing cwd and cancellation signal.

        Returns:
            Tool result containing stdout and stderr.

        """

        timeout_s = _to_float(input.get("timeout_ms"), 120000.0) / 1000.0
        if ctx.tool_timeout_s is not None:
            timeout_s = min(timeout_s, max(ctx.tool_timeout_s - 1, ctx.tool_timeout_s / 2))
        registry = getattr(ctx._session, "_processes", None)
        if registry is not None and callable(getattr(self._backend, "start", None)):
            return await registry.run(self, input, ctx, timeout_s)
        result = await self._backend.run(
            str(input["command"]),
            cwd=ctx.cwd,
            timeout_s=timeout_s,
            signal=ctx.signal,
        )
        return self._render(result)

    @staticmethod
    def _render(result: Any) -> ToolResult:
        status = (
            f"command timed out (exit {result.returncode})"
            if result.timed_out
            else f"command aborted (exit {result.returncode})"
            if result.aborted
            else f"Exited {result.returncode}"
        )
        if result.signal is not None:
            status += f" (signal {result.signal})"
        paths = {
            key: value
            for key, value in (
                ("stdout_path", result.stdout_path),
                ("stderr_path", result.stderr_path),
            )
            if value
        }
        content = result.stdout + (("\n[stderr]\n" + result.stderr) if result.stderr else "")
        content += f"\n[{status}]"
        if result.truncated:
            content += "\n[output truncated]"
        for key, path in paths.items():
            content += f"\n[{key}: {path}]"
        return ToolResult(
            content=content,
            summary=status,
            is_error=result.returncode != 0 or result.timed_out or result.aborted,
            truncated=result.truncated,
            metadata=paths,
        )

    def summarize(self, input: dict[str, object]) -> str:
        """Summarize a shell command.

        Args:
            input: Validated command request.

        Returns:
            Truncated command summary.
        """

        return f"Bash: {str(input['command'])[:60]}"


# ---------------------------------------------------------------------------
# Grep (ripgrep-first, Python fallback)
# ---------------------------------------------------------------------------

_RG_PATH = shutil.which("rg")

_DEFAULT_HEAD_LIMIT = 250
_VCS_IGNORE_DIRS = {".git", ".hg", ".svn"}

_TYPE_GLOBS: dict[str, str] = {
    "ts": "**/*.{ts,tsx}",
    "js": "**/*.{js,jsx,mjs,cjs}",
    "py": "**/*.py",
    "go": "**/*.go",
    "rs": "**/*.rs",
    "md": "**/*.{md,markdown}",
    "json": "**/*.json",
    "yaml": "**/*.{yaml,yml}",
    "html": "**/*.{html,htm}",
    "css": "**/*.css",
    "sh": "**/*.sh",
    "c": "**/*.{c,h}",
    "cpp": "**/*.{cpp,cc,cxx,hpp,hxx}",
    "java": "**/*.java",
    "rb": "**/*.rb",
    "php": "**/*.php",
    "swift": "**/*.swift",
    "kt": "**/*.kt",
}


def _to_int(value: object, default: int) -> int:
    if value is None:
        return default
    return int(cast(Any, value))


def _to_float(value: object, default: float) -> float:
    if value is None:
        return default
    return float(cast(Any, value))


def _is_binary(buf: bytes) -> bool:
    return b"\x00" in buf[:8192]


def _load_gitignore_entries(root: str) -> list[str]:
    entries: list[str] = []
    cur = Path(root).resolve()
    seen: set[str] = set()
    while True:
        key = str(cur)
        if key in seen:
            break
        seen.add(key)
        gi = cur / ".gitignore"
        if gi.is_file():
            try:
                entries.extend(
                    line.strip()
                    for line in gi.read_text(errors="replace").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                )
            except Exception:
                pass
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return entries


def _matches_gitignore(rel_path: str, entries: list[str]) -> bool:
    for pattern in entries:
        if pattern.endswith("/"):
            if rel_path.startswith(pattern):
                return True
            if rel_path.startswith(pattern[:-1] + "/"):
                return True
            if rel_path == pattern[:-1]:
                return True
        elif rel_path == pattern or rel_path.startswith(pattern + "/"):
            return True
        elif pattern.startswith("/") and rel_path == pattern[1:]:
            return True
        elif "*" in pattern or "?" in pattern or "[" in pattern:
            if Path(rel_path).match(pattern):
                return True
    return False


def _is_vcs_dir(part: str) -> bool:
    return part in _VCS_IGNORE_DIRS


def _run_grep_via_rg(
    args: list[str],
    search_root: str,
    timeout: float = 120.0,
) -> tuple[str, int]:
    rg_path = _RG_PATH
    if rg_path is None:
        raise ToolExecutionError("ripgrep is not available")
    proc = asyncio.run(
        asyncio.create_subprocess_exec(
            rg_path,
            *args,
            cwd=search_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    )
    stdout, _ = asyncio.run(asyncio.wait_for(proc.communicate(), timeout))
    return stdout.decode(errors="replace"), proc.returncode or 0


async def _run_grep_via_rg_async(
    args: list[str],
    search_root: str,
    timeout: float = 120.0,
) -> tuple[str, int, str]:
    rg_path = _RG_PATH
    if rg_path is None:
        raise ToolExecutionError("ripgrep is not available")
    proc = await asyncio.create_subprocess_exec(
        rg_path,
        *args,
        cwd=search_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    return (
        stdout.decode(errors="replace"),
        proc.returncode or 0,
        stderr.decode(errors="replace"),
    )


def _grep_fallback(
    pattern: str,
    search_root: str,
    *,
    output_mode: str = "files_with_matches",
    ignore_case: bool = False,
    multiline: bool = False,
    show_line_numbers: bool = False,
    glob_filter: str | None = None,
    type_filter: str | None = None,
    context_before: int = 0,
    context_after: int = 0,
) -> str:
    flags = 0
    if ignore_case:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.DOTALL
    flags |= re.MULTILINE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return f"Invalid regex: {exc}"

    gitignore_entries = _load_gitignore_entries(search_root)

    actual_glob = glob_filter or "**/*"
    if type_filter:
        type_glob = _TYPE_GLOBS.get(type_filter)
        if type_glob:
            actual_glob = "{" + actual_glob + "," + type_glob + "}" if glob_filter else type_glob

    all_files = _glob_files(search_root, actual_glob)
    matched_files: list[tuple[str, list[int], list[str]]] = []

    for rel_path in all_files:
        if _is_vcs_dir(rel_path.split("/")[0]):
            continue
        if _matches_gitignore(rel_path, gitignore_entries):
            continue
        abs_path = Path(search_root) / rel_path
        try:
            buf = abs_path.read_bytes()
        except Exception:
            continue
        if _is_binary(buf):
            continue
        text = buf.decode(errors="replace")
        match_lines: list[int] = []
        for lineno, line in enumerate(text.split("\n"), 1):
            if regex.search(line):
                match_lines.append(lineno)
        if match_lines:
            matched_files.append((rel_path, match_lines, text.split("\n")))

    if not matched_files:
        return ""

    if output_mode == "files_with_matches":
        return "\n".join(r for r, _, _ in matched_files)
    if output_mode == "count":
        return "\n".join(f"{r}:{len(ml)}" for r, ml, _ in matched_files)

    out_lines: list[str] = []
    for rel_path, match_lines, file_lines in matched_files:
        ranges: list[tuple[int, int]] = []
        for ml in match_lines:
            start = max(0, ml - 1 - context_before)
            end = min(len(file_lines) - 1, ml - 1 + context_after)
            if ranges and start <= ranges[-1][1] + 1:
                ranges[-1] = (
                    ranges[-1][0],
                    max(ranges[-1][1], end),
                )
            else:
                ranges.append((start, end))
        prev_end = -1
        for r_start, r_end in ranges:
            if prev_end >= 0 and r_start > prev_end + 1:
                out_lines.append("--")
            for i in range(r_start, r_end + 1):
                line_text = file_lines[i] if i < len(file_lines) else ""
                is_match = i + 1 in match_lines
                sep = ":" if is_match else "-"
                if show_line_numbers:
                    out_lines.append(f"{rel_path}{sep}{i + 1}{sep}{line_text}")
                else:
                    out_lines.append(f"{rel_path}:{line_text}")
            prev_end = r_end
    return "\n".join(out_lines)


def _glob_files(root: str, pattern: str) -> list[str]:
    _validate_glob_pattern(pattern)
    root_path = Path(root).resolve()
    paths = glob.glob(pattern, root_dir=root, recursive=True)
    result: list[str] = []
    for p in paths:
        path = Path(p)
        candidate = path if path.is_absolute() else root_path / path
        try:
            rel = candidate.resolve().relative_to(root_path).as_posix()
        except ValueError:
            continue
        if rel and not rel.startswith("."):
            result.append(rel)
    return sorted(result)


def _validate_glob_pattern(pattern: str) -> None:
    """Reject glob patterns that can escape their search root."""

    if Path(pattern).is_absolute():
        raise ToolExecutionError("glob pattern must be relative to the search root")
    if any(segment == ".." for segment in pattern.replace("\\", "/").split("/")):
        raise ToolExecutionError("glob pattern cannot contain parent-directory segments")


class GrepTool:
    """Search workspace file contents with regex matching."""

    name = "Grep"
    description = (
        "Searches file contents by regex. Uses ripgrep when available;"
        " falls back to pure-Python. Output modes: files_with_matches"
        " (default), content, count. Supports -i, -n, -A/-B/-C context,"
        " multiline, glob/type filter, head_limit."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": "Root directory. Defaults to cwd.",
            },
            "glob": {
                "type": "string",
                "description": "File filter glob, e.g. '**/*.ts'.",
            },
            "type": {
                "type": "string",
                "description": "File type alias (ts, js, py, ...).",
            },
            "output_mode": {
                "type": "string",
                "enum": ["files_with_matches", "content", "count"],
                "description": "Output mode. Default: files_with_matches.",
            },
            "-i": {
                "type": "boolean",
                "description": "Case-insensitive matching.",
            },
            "-n": {
                "type": "boolean",
                "description": "Show line numbers (content mode).",
            },
            "-A": {
                "type": "integer",
                "description": "Lines of context after match.",
            },
            "-B": {
                "type": "integer",
                "description": "Lines of context before match.",
            },
            "-C": {
                "type": "integer",
                "description": "Lines of context around match.",
            },
            "multiline": {
                "type": "boolean",
                "description": "Enable multiline mode.",
            },
            "head_limit": {
                "type": "integer",
                "description": "Max output lines. Default 250.",
            },
        },
        "required": ["pattern"],
    }
    scope: ToolScope = "read"
    parallel: bool = True

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        """Validate regex search options.

        Args:
            raw: Provider-supplied search request.

        Returns:
            Normalized search options.

        Raises:
            ValueError: If the pattern is missing or empty.
        """

        pattern = require_str(raw, "pattern")
        if not pattern:
            raise ToolExecutionError("pattern must be non-empty")
        result: dict[str, object] = {"pattern": pattern}
        for opt in (
            "path",
            "glob",
            "type",
            "output_mode",
        ):
            v = raw.get(opt)
            if v is not None:
                result[opt] = str(v)
        for flag in ("-i", "-n", "multiline"):
            v = raw.get(flag)
            if v is not None:
                result[flag] = bool(v)
        for num in ("-A", "-B", "-C", "head_limit"):
            v = raw.get(num)
            if v is not None:
                result[num] = _to_int(v, 0)
        return result

    def summarize(self, input: dict[str, object]) -> str:
        """Summarize a regex search.

        Args:
            input: Validated search options.

        Returns:
            Human-readable summary including output mode.
        """

        mode = input.get("output_mode", "files_with_matches")
        return f"Grep {input['pattern']} ({mode})"

    def resources(self, input: dict[str, object]) -> list[ResourceAccess]:
        """Describe the filesystem area searched.

        Args:
            input: Validated search options.

        Returns:
            One read resource descriptor.
        """

        path = str(input.get("path", "") or ".")
        return [ResourceAccess(resource=f"fs:{path}", mode="read")]

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        """Search files and return capped matching output.

        Args:
            input: Validated search options.
            ctx: Runtime context containing workspace root.

        Returns:
            Tool result containing matches or a search error.
        """

        pattern = str(input["pattern"])
        path = str(input.get("path", "") or "")
        try:
            search_root = str(resolve_under(ctx.cwd, path or "."))
        except ToolExecutionError as exc:
            return ToolResult(
                content=f"Grep error: {exc}",
                summary="grep error",
                is_error=True,
            )
        output_mode = str(input.get("output_mode", "files_with_matches"))
        head_limit = _to_int(input.get("head_limit"), _DEFAULT_HEAD_LIMIT)
        ignore_case = bool(input.get("-i", False))
        multiline = bool(input.get("multiline", False))
        show_ln = bool(input.get("-n", False))
        ctx_a = _to_int(input.get("-A"), 0) or 0
        ctx_b = _to_int(input.get("-B"), 0) or 0
        ctx_c = _to_int(input.get("-C"), 0) or 0
        glob_filter = str(input.get("glob", "") or "")
        type_filter = str(input.get("type", "") or "")

        if _RG_PATH:
            args = ["--max-columns", "500"]
            if output_mode == "files_with_matches":
                args.append("--files-with-matches")
            elif output_mode == "count":
                args.append("--count")
            if ignore_case:
                args.append("--ignore-case")
            if show_ln and output_mode == "content":
                args.append("--line-number")
            if multiline:
                args.append("--multiline")
                args.append("--multiline-dotall")
            if ctx_c > 0:
                args.extend(["-C", str(ctx_c)])
            else:
                if ctx_a > 0:
                    args.extend(["-A", str(ctx_a)])
                if ctx_b > 0:
                    args.extend(["-B", str(ctx_b)])
            if glob_filter:
                args.append(f"--glob={glob_filter}")
            if type_filter:
                args.append(f"--type={type_filter}")
            args.extend(["-e", pattern, "--", search_root])
            try:
                raw_out, rg_code, rg_err = await _run_grep_via_rg_async(args, search_root)
            except Exception as exc:
                return ToolResult(
                    content=f"Grep error: {exc}",
                    summary="grep error",
                    is_error=True,
                )
            # rg exit codes: 0=matches, 1=no matches, 2=error (e.g. bad regex).
            # Surface code 2 as an error instead of a silent "No matches found.".
            if rg_code >= 2:
                detail = rg_err.strip() or "ripgrep reported an error"
                return ToolResult(
                    content=f"Invalid regex / grep error: {detail}",
                    summary="grep error",
                    is_error=True,
                )
        else:
            try:
                raw_out = _grep_fallback(
                    pattern,
                    search_root,
                    output_mode=output_mode,
                    ignore_case=ignore_case,
                    multiline=multiline,
                    show_line_numbers=show_ln,
                    glob_filter=glob_filter or None,
                    type_filter=type_filter or None,
                    context_before=ctx_c or ctx_b,
                    context_after=ctx_c or ctx_a,
                )
            except ToolExecutionError as exc:
                return ToolResult(
                    content=f"Grep error: {exc}",
                    summary="grep error",
                    is_error=True,
                )

        if not raw_out or raw_out.strip() == "":
            return ToolResult(content="No matches found.", summary="no matches")

        lines = raw_out.split("\n")
        limited = "\n".join(lines[:head_limit])
        if len(lines) > head_limit:
            limited += f"\n… (truncated to {head_limit} of {len(lines)} lines)"
        if len(limited) > OUTPUT_CAP:
            limited = limited[:OUTPUT_CAP] + f"\n… (output capped at {OUTPUT_CAP} chars)"
        return ToolResult(
            content=limited,
            summary=f"Grep matched {min(len(lines), head_limit)} line(s)",
        )


# ---------------------------------------------------------------------------
# Glob (ripgrep --files first, Python fallback)
# ---------------------------------------------------------------------------

_GLOB_CAP = 1000


def _pattern_targets_dotfiles(pattern: str) -> bool:
    """True when the glob pattern explicitly targets a dotfile/dotdir.

    A pattern targets dotfiles when any of its path segments begins with a
    literal ``.`` (e.g. ``.env*`` or ``.github/**/*.yml``). In that case the
    default "hide dotfiles" behaviour must be suppressed.
    """
    return any(seg.startswith(".") for seg in pattern.split("/") if seg)


class GlobTool:
    """Find workspace files matching a glob pattern."""

    name = "Glob"
    description = (
        "Finds files matching a glob pattern. Uses ripgrep for speed"
        " when available; falls back to glob. Results sorted by"
        " modification time (most recent first), capped at 1000."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "glob_pattern": {
                "type": "string",
                "description": "Glob pattern, e.g. 'src/**/*.ts'.",
            },
            "target_directory": {
                "type": "string",
                "description": "Directory to search. Defaults to cwd.",
            },
        },
        "required": ["glob_pattern"],
    }
    scope: ToolScope = "read"
    parallel: bool = True

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        """Validate a glob pattern and optional target directory.

        Args:
            raw: Provider-supplied glob request.

        Returns:
            Normalized glob options.

        Raises:
            ToolExecutionError: If the pattern is missing or empty.
        """

        pattern = raw.get("glob_pattern")
        if not isinstance(pattern, str) or pattern == "":
            raise ToolExecutionError("glob_pattern must be non-empty")
        result: dict[str, object] = {"glob_pattern": pattern}
        target = raw.get("target_directory")
        if target is not None:
            result["target_directory"] = str(target)
        return result

    def summarize(self, input: dict[str, object]) -> str:
        """Summarize a glob search.

        Args:
            input: Validated glob options.

        Returns:
            Human-readable pattern summary.
        """

        return f"Glob {input['glob_pattern']}"

    def resources(self, input: dict[str, object]) -> list[ResourceAccess]:
        """Describe the filesystem area searched.

        Args:
            input: Validated glob options.

        Returns:
            One read resource descriptor.
        """

        path = str(input.get("target_directory", "") or ".")
        return [ResourceAccess(resource=f"fs:{path}", mode="read")]

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        """Find and sort matching files with a bounded result count.

        Args:
            input: Validated glob options.
            ctx: Runtime context containing workspace root.

        Returns:
            Tool result containing matching paths or an error.
        """

        pattern = str(input["glob_pattern"])
        path_str = str(input.get("target_directory", "") or "")
        try:
            root = str(resolve_under(ctx.cwd, path_str or "."))
        except ToolExecutionError as exc:
            return ToolResult(
                content=f"Glob error: {exc}",
                summary="glob error",
                is_error=True,
            )

        # Only hide dotfiles when the user's pattern doesn't itself target them.
        include_dotfiles = _pattern_targets_dotfiles(pattern)

        if _RG_PATH:
            try:
                _validate_glob_pattern(pattern)
            except ToolExecutionError as exc:
                return ToolResult(content=f"Glob error: {exc}", summary="glob error", is_error=True)
            args = ["--files", f"--glob={pattern}"]
            if not include_dotfiles:
                args.append("--glob=!.*")
            args.extend(["--", root])
            try:
                raw_out, _, _ = await _run_grep_via_rg_async(args, root)
            except Exception as exc:
                return ToolResult(
                    content=f"Glob error: {exc}",
                    summary="glob error",
                    is_error=True,
                )
            files = [line.strip() for line in raw_out.split("\n") if line.strip()]
        else:
            try:
                _validate_glob_pattern(pattern)
                entries = glob.glob(pattern, root_dir=root, recursive=True)
            except ToolExecutionError as exc:
                return ToolResult(content=f"Glob error: {exc}", summary="glob error", is_error=True)
            root_path = Path(root).resolve()
            safe_entries: list[str] = []
            for entry in entries:
                candidate = Path(entry)
                candidate = candidate if candidate.is_absolute() else root_path / candidate
                try:
                    safe_entries.append(candidate.resolve().relative_to(root_path).as_posix())
                except ValueError:
                    continue
            if include_dotfiles:
                files = sorted(safe_entries)
            else:
                files = sorted(p for p in safe_entries if not p.startswith("."))

        if not files:
            return ToolResult(content="No matches found.", summary="no matches")

        mtime_pairs: list[tuple[str, float]] = []
        for f in files:
            full = Path(root) / f
            try:
                mtime_pairs.append((f, full.stat().st_mtime))
            except OSError:
                mtime_pairs.append((f, 0.0))
        mtime_pairs.sort(key=lambda x: x[1], reverse=True)
        sorted_files = [f for f, _ in mtime_pairs]

        total = len(sorted_files)
        capped = sorted_files[:_GLOB_CAP]
        output = "\n".join(capped)
        if total > _GLOB_CAP:
            output += f"\n… (truncated to {_GLOB_CAP} of {total} results)"
        return ToolResult(
            content=output,
            summary=f"{min(total, _GLOB_CAP)} file(s) matched",
        )
