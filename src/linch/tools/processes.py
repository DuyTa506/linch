"""Ephemeral, run-owned shell handles and their model-facing controls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .base import ToolContext, ToolResult, ToolScope, require_str
from .builtin import _to_int


async def _settle(task: asyncio.Task[Any]) -> Any:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


@dataclass(slots=True)
class _Process:
    handle: Any
    timer: asyncio.Task[None] | None = None
    timed_out: bool = False
    quiet: int = 0
    aborted: bool = False

    async def limit(self, seconds: float, signal: Any) -> None:
        try:
            async with asyncio.timeout(seconds):
                await (signal.wait() if signal is not None else asyncio.Future())
        except TimeoutError:
            self.timed_out = await self.handle.wait(0) is None
        else:
            self.aborted = True
        await _settle(asyncio.create_task(self.handle.kill()))


@dataclass(slots=True)
class ProcessRegistry:
    run_id: str
    entries: list[_Process] = field(default_factory=list)
    ids: dict[str, _Process] = field(default_factory=dict)
    closed: bool = False
    cleaned: bool = False
    unwound: asyncio.Event = field(default_factory=asyncio.Event)
    _closing: asyncio.Task[None] | None = None

    async def close(self) -> None:
        self.closed = True
        if self.cleaned:
            return
        if self._closing is None or self._closing.done():
            self._closing = asyncio.create_task(self._close())
        await _settle(self._closing)

    async def _close(self) -> None:
        for entry in self.entries:
            if entry.timer is not None:
                entry.timer.cancel()
        await asyncio.gather(*(e.timer for e in self.entries if e.timer), return_exceptions=True)
        failures: list[Exception] = []
        for entry in self.entries:
            try:
                await entry.handle.kill()
                result = await entry.handle.wait(None)
                for path in (result.stdout_path, result.stderr_path):
                    if path:
                        Path(path).unlink(missing_ok=True)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ExceptionGroup("Process cleanup failed; retry session close", failures)
        self.cleaned = True

    async def run(
        self, tool: Any, input: dict[str, object], ctx: ToolContext, timeout: float
    ) -> ToolResult:
        if self.closed or self.run_id != ctx.run_id:
            return ToolResult(content="Process run has ended.", is_error=True)
        from ..abort import throw_if_aborted

        throw_if_aborted(ctx.signal)
        running = 0
        for entry in self.entries:
            running += getattr(entry.handle, "_group_alive", await entry.handle.wait(0) is None)
        handle = await tool._backend.start(str(input["command"]), cwd=ctx.cwd)
        entry = _Process(handle)
        self.entries.append(entry)
        self.cleaned = False
        if self.closed or self.run_id != ctx.run_id:
            await _settle(asyncio.create_task(self._close()))
            return ToolResult(content="Process run has ended.", is_error=True)
        entry.timer = asyncio.create_task(entry.limit(tool.max_runtime_ms / 1000, ctx.signal))
        try:
            result = await handle.wait(0 if input.get("run_in_background") else timeout)
            if running >= tool.max_processes and getattr(handle, "_group_alive", result is None):
                await _settle(asyncio.create_task(handle.kill()))
                return tool._render(replace(await handle.wait(None), timed_out=True))
            if result is not None:
                return await _render(entry, "", 0)
            id = f"p{len(self.ids) + 1}"
            self.ids[id] = entry
            return await _render(entry, id, 0)
        except BaseException:
            await _settle(asyncio.create_task(handle.kill()))
            raise


async def _render(entry: _Process, id: str, timeout: float, waiting: bool = False) -> ToolResult:
    from .builtin import BashTool

    result = await entry.handle.wait(timeout)
    read = entry.handle.read()
    entry.quiet = entry.quiet + int(waiting) if not read.stdout and not read.stderr else 0
    if result is None:
        text = read.stdout + (("\n[stderr]\n" + read.stderr) if read.stderr else "")
        text += f"\n[Process {id} still running; use BashProcess]"
        if entry.quiet >= 3:
            text += "\n[No new output; consider stopping this process.]"
        paths = {
            key: getattr(read, key) for key in ("stdout_path", "stderr_path") if getattr(read, key)
        }
        text += "".join(f"\n[{k}: {v}]" for k, v in paths.items())
        if read.lossy:
            text += "\n[output truncated]"
        return ToolResult(content=text, truncated=read.lossy, metadata={"process_id": id, **paths})
    result = replace(result, stdout=read.stdout, stderr=read.stderr)
    result.timed_out, result.aborted = entry.timed_out, entry.aborted
    return BashTool._render(result)


class BashProcessTool:
    """Inspect or stop a command already approved through Bash."""

    name = "BashProcess"
    description = "Wait for new output, inspect status, or kill a Bash process by id."
    input_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["wait", "kill", "status"]},
            "id": {"type": "string"},
            "wait_ms": {"type": "integer"},
        },
        "required": ["action", "id"],
    }
    scope: ToolScope = "exec"
    parallel = False

    def validate(self, raw: dict[str, object]) -> dict[str, object]:
        action = require_str(raw, "action")
        if action not in {"wait", "kill", "status"}:
            raise ValueError("action must be wait, kill, or status")
        return {
            "action": action,
            "id": require_str(raw, "id"),
            "wait_ms": min(1800000, max(0, _to_int(raw.get("wait_ms"), 1000))),
        }

    async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
        registry = getattr(ctx._session, "_processes", None)
        id = str(input["id"])
        if (
            registry is None
            or registry.closed
            or registry.run_id != ctx.run_id
            or id not in registry.ids
        ):
            return ToolResult(content=f"Process {id} is gone; start a new command.", is_error=True)
        entry = registry.ids[id]
        if input["action"] == "kill":
            await _settle(asyncio.create_task(entry.handle.kill()))
        timeout = _to_int(input.get("wait_ms"), 1000) / 1000 if input["action"] == "wait" else 0
        if ctx.tool_timeout_s is not None:
            timeout = min(timeout, max(ctx.tool_timeout_s - 1, ctx.tool_timeout_s / 2))
        return await _render(entry, id, timeout, input["action"] == "wait")

    def summarize(self, input: dict[str, object]) -> str:
        return f"BashProcess {input['action']} {input['id']}"
