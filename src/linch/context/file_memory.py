"""A stable, run-scoped memory index on the existing virtual filesystem."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..filesystem.backend import resolve_filesystem_backend
from ..types import SystemBlock
from .builder import ContextBuildResult, ContextBuildTurn

if TYPE_CHECKING:
    from ..hooks.contexts import AgentStopContext

_INSTRUCTION = (
    "Memories live under /memories/, one file per fact. "
    "MEMORY.md is a one-line-per-file index. Use the file tools to read and maintain "
    "memories; update or delete stale facts."
)


class FileMemoryContextBuilder:
    """Inject ``/memories/MEMORY.md`` once per run, capped at 200 lines.

    Install through ``ContextInjectionHook`` to release the cached index when
    the run ends. Dependencies override session and execution filesystems,
    matching the existing file tools. No memory is written by this builder.
    """

    def __init__(self) -> None:
        """Create a builder with an empty per-run memory-index cache."""
        self._by_run: dict[str, str] = {}

    async def build(self, turn: ContextBuildTurn) -> ContextBuildResult:
        """Inject the run's memory index into ephemeral system context.

        Args:
            turn: Active session and run dependencies used to locate the filesystem.

        Returns:
            System blocks containing the memory instructions and, when present,
            the first 200 lines of ``/memories/MEMORY.md``.

        Raises:
            RuntimeError: If the session has no active run.
        """
        run_id = turn.session.active_run_id
        if run_id is None:
            raise RuntimeError("File memory requires an active run")
        if run_id not in self._by_run:
            backend = resolve_filesystem_backend(turn.deps)
            if backend is None:
                backend = turn.session.filesystem
            if backend is None:
                execution = getattr(turn.session.agent, "execution_backend", None)
                backend = getattr(execution, "fs", None)
            index = ""
            if backend is not None:
                try:
                    index = await backend.read("/memories/MEMORY.md", limit=201)
                except FileNotFoundError:
                    pass
            lines = index.splitlines()
            index = "\n".join(lines[:200])
            if len(lines) > 200:
                index += "\n[MEMORY.md truncated to 200 lines.]"
            self._by_run[run_id] = index

        blocks = [SystemBlock(text=_INSTRUCTION, cacheable=True)]
        index = self._by_run[run_id]
        if index:
            blocks.append(SystemBlock(text=f"/memories/MEMORY.md:\n{index}", cacheable=True))
        return ContextBuildResult(system_blocks=blocks)

    def on_agent_stop(self, ctx: AgentStopContext) -> None:
        """Discard the cached memory index for a completed run.

        Args:
            ctx: Agent-stop context identifying the run to remove.
        """
        self._by_run.pop(ctx.run_id, None)
