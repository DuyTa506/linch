"""Ephemeral, run-owned shell handles and their model-facing controls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

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
    stopped: bool = False

    async def stop(self) -> None:
        await self.handle.kill()
        self.stopped = True

    async def limit(self, seconds: float, signal: Any) -> None:
        try:
            async with asyncio.timeout(seconds):
                await (signal.wait() if signal is not None else asyncio.Future())
        except TimeoutError:
            pending = await self.handle.wait(0) is None
            if getattr(self.handle, "_leader_exited", False):
                return
            self.timed_out = pending
        else:
            if getattr(self.handle, "_leader_exited", False):
                return
            self.aborted = True
        if self.timed_out or self.aborted:
            pending = await self.handle.wait(0) is None
            if getattr(self.handle, "_leader_exited", False):
                self.timed_out = self.aborted = False
                return
            if pending:
                await _settle(asyncio.create_task(self.stop()))


@dataclass(slots=True)
class ProcessRegistry:
    run_id: str
    entries: list[_Process] = field(default_factory=list)
    ids: dict[str, _Process] = field(default_factory=dict)
    spills: set[str] = field(default_factory=set)
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
                self._remember_spills(result)
            except Exception as exc:
                failures.append(exc)
        for path in tuple(self.spills):
            try:
                await asyncio.to_thread(Path(path).unlink, missing_ok=True)
                self.spills.remove(path)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ExceptionGroup("Process cleanup failed; retry session close", failures)
        self.cleaned = True

    def _remember_spills(self, result: Any) -> None:
        self.spills.update(path for path in (result.stdout_path, result.stderr_path) if path)

    async def _retire(self, entry: _Process, result: Any) -> bool:
        alive = getattr(entry.handle, "_group_alive", None)
        if alive is None:
            alive = result is None
        if (alive and not entry.stopped) or result is None:
            return False
        self._remember_spills(result)
        if entry.timer is not None:
            entry.timer.cancel()
        if entry in self.entries:
            self.entries.remove(entry)
        return True

    def _trim_history(self) -> None:
        active = {id(entry) for entry in self.entries}
        finished = [key for key, entry in self.ids.items() if id(entry) not in active]
        for key in finished[:-128]:
            del self.ids[key]

    async def run(
        self, tool: Any, input: dict[str, object], ctx: ToolContext, timeout: float
    ) -> ToolResult:
        if self.closed or self.run_id != ctx.run_id:
            return ToolResult(content="Process run has ended.", is_error=True)
        from ..abort import throw_if_aborted

        throw_if_aborted(ctx.signal)
        for entry in self.entries[:]:
            await self._retire(entry, await entry.handle.wait(0))
        self._trim_history()
        running = len(self.entries)
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
            alive = getattr(handle, "_group_alive", None)
            if running >= tool.max_processes and (result is None if alive is None else alive):
                await _settle(asyncio.create_task(entry.stop()))
                result = await handle.wait(None)
                await self._retire(entry, result)
                return tool._render(replace(result, timed_out=True))
            if result is not None:
                rendered = await _render(entry, "", 0)
                await self._retire(entry, result)
                return rendered
            while True:
                id = f"p{uuid4().hex[:8]}"
                if id not in self.ids:
                    break
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
            await _settle(asyncio.create_task(entry.stop()))
        timeout = _to_int(input.get("wait_ms"), 1000) / 1000 if input["action"] == "wait" else 0
        if ctx.tool_timeout_s is not None:
            timeout = min(timeout, max(ctx.tool_timeout_s - 1, ctx.tool_timeout_s / 2))
        result = await _render(entry, id, timeout, input["action"] == "wait")
        if input["action"] == "kill":
            result.is_error = False
        await registry._retire(entry, await entry.handle.wait(0))
        registry._trim_history()
        return result

    def summarize(self, input: dict[str, object]) -> str:
        return f"BashProcess {input['action']} {input['id']}"
