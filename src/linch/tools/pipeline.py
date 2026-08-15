"""Composable tool-execution pipeline — the ``tools/*`` waterfall seam.

Ported from dsh's tool pipeline (deepseek-harness/packages/core/tools/src/index.ts).
linch already routes *pre*/*post* tool extension through hooks and ships built-in
timeout/retry, so the genuinely new capability this adds is the **around-execution
seam**: third parties register wrappers on ``tools/execute`` that wrap the tool
body (replacing its signal, timing it, short-circuiting it) without editing the
scheduler.

A :class:`ToolPipeline` owns a kernel :class:`~linch.kernel.EventBus`. Attach one
to an agent as ``agent.tool_pipeline`` and register listeners; the scheduler runs
the ``pre → execute → post`` lifecycle around each tool body. With no listeners
registered the scheduler skips the pipeline entirely, so the default path is
byte-identical to before this existed.

An execute wrapper is ``async def wrapper(execution: ToolExecution, next) -> Any``
and MUST ``await next()`` to run the wrapped body (or return without it to
short-circuit). Pre listeners may likewise short-circuit dispatch. Post
listeners accept the candidate result with ``await next()`` or replace it by
returning another value.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from linch.kernel import Disposable, EventBus

PRE_EXECUTE = "tools/pre-execute"
EXECUTE = "tools/execute"
POST_EXECUTE = "tools/post-execute"

Terminal = Callable[[], Any]


@dataclass(slots=True)
class ToolExecution:
    """Execution record threaded through the tool pipeline.

    ``signal`` and ``metadata`` are extension seams. ``tool``, ``input``, and
    ``ctx`` describe the already-authorized call; scheduler dispatch fails
    closed if a listener changes them.
    """

    tool_name: str
    tool_use_id: str
    input: dict[str, Any]
    ctx: Any
    tool: Any = None
    signal: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ToolPipeline:
    """A per-agent seam of composable ``tools/*`` waterfalls over the kernel bus."""

    __slots__ = ("_bus",)

    resume_policy_id = "linch.tools.pipeline"
    resume_policy_version = "1"

    def __init__(self) -> None:
        self._bus = EventBus()

    def on_pre_execute(self, listener: Any) -> Disposable:
        return self._bus.on(PRE_EXECUTE, listener)

    def on_execute(self, wrapper: Any) -> Disposable:
        """Register an around-execution wrapper; returns a disposer.

        Wrappers registered later run *inside* earlier ones (the first registered
        is outermost), so a metrics wrapper registered before a timeout wrapper
        times the timeout too.
        """
        return self._bus.on(EXECUTE, wrapper)

    def on_post_execute(self, listener: Any) -> Disposable:
        return self._bus.on(POST_EXECUTE, listener)

    def has_execute_listeners(self) -> bool:
        return self._bus.has_listeners(EXECUTE)

    def has_listeners(self) -> bool:
        """Whether any pipeline phase has an active listener."""
        return any(self._bus.has_listeners(phase) for phase in (PRE_EXECUTE, EXECUTE, POST_EXECUTE))

    @property
    def resume_policy_config(self) -> dict[str, list[dict[str, Any]]]:
        """Stable, ordered listener identity used by durable run contracts.

        Custom listeners must declare a non-empty ``resume_policy_id``. They
        may also declare ``resume_policy_version`` and JSON-safe
        ``resume_policy_config``. Requiring the host-owned id fails closed for
        anonymous closures whose behavior cannot otherwise be compared across
        processes.
        """
        return {
            phase: [
                _listener_resume_identity(listener, phase=phase)
                for listener in self._bus._listeners.get(phase, ())
            ]
            for phase in (PRE_EXECUTE, EXECUTE, POST_EXECUTE)
        }

    async def run(self, execution: ToolExecution, terminal: Terminal) -> Any:
        """Run the complete pre → execute → post lifecycle.

        A pre listener that does not call ``next()`` supplies the candidate
        result and skips dispatch. Post listeners receive that candidate (or
        the executed result); ``await next()`` accepts/delegates it, while the
        value returned by a listener is the authoritative replacement.
        """

        async def _execute() -> Any:
            if self._bus.has_listeners(EXECUTE):
                return await self.run_execute(execution, terminal)
            return await terminal()

        if self._bus.has_listeners(PRE_EXECUTE):
            proceed = object()

            async def _proceed() -> object:
                return proceed

            pre_result = await self.run_pre_execute(execution, _proceed)
            result = await _execute() if pre_result is proceed else pre_result
        else:
            result = await _execute()

        if not self._bus.has_listeners(POST_EXECUTE):
            return result

        async def _accept_result() -> Any:
            return result

        return await self.run_post_execute(execution, result, _accept_result)

    async def run_execute(self, execution: ToolExecution, terminal: Terminal) -> Any:
        """Run the ``tools/execute`` waterfall with *terminal* as the tool body."""
        return await self._bus.waterfall(EXECUTE, execution, next=terminal)

    async def run_pre_execute(self, execution: ToolExecution, terminal: Terminal) -> Any:
        return await self._bus.waterfall(PRE_EXECUTE, execution, next=terminal)

    async def run_post_execute(
        self, execution: ToolExecution, result: Any, terminal: Terminal
    ) -> Any:
        return await self._bus.waterfall(POST_EXECUTE, execution, result, next=terminal)


def _listener_resume_identity(listener: Any, *, phase: str) -> dict[str, Any]:
    policy_id = getattr(listener, "resume_policy_id", getattr(listener, "policy_id", None))
    if not isinstance(policy_id, str) or not policy_id:
        name = getattr(listener, "__qualname__", listener.__class__.__qualname__)
        raise ValueError(
            f"durable runs with {phase} listener {name!r} require "
            "listener.resume_policy_id (plus optional resume_policy_version/"
            "resume_policy_config)"
        )
    policy_version = getattr(
        listener,
        "resume_policy_version",
        getattr(listener, "policy_version", None),
    )
    callable_module = getattr(listener, "__module__", listener.__class__.__module__)
    callable_name = getattr(listener, "__qualname__", listener.__class__.__qualname__)
    config = getattr(listener, "resume_policy_config", None)
    fingerprint = getattr(listener, "resume_policy_fingerprint", None)
    if config is None and not (isinstance(fingerprint, str) and fingerprint):
        raise ValueError(
            f"durable runs with {phase} listener {callable_name!r} require non-None "
            "listener.resume_policy_config or a non-empty "
            "listener.resume_policy_fingerprint"
        )
    semantic_identity = config if config is not None else {"fingerprint": fingerprint}
    try:
        json.dumps(
            semantic_identity,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"durable {phase} listener {callable_name!r} resume policy config/fingerprint "
            f"must be JSON-safe: {exc}"
        ) from exc
    return {
        "type": f"{listener.__class__.__module__}.{listener.__class__.__qualname__}",
        "name": f"{callable_module}.{callable_name}",
        "policy_id": policy_id,
        "policy_version": policy_version,
        "config": config,
        "fingerprint": fingerprint,
    }
