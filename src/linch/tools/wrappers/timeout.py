"""A ``tools/execute`` wrapper imposing a per-pipeline deadline on the tool body.

Distinct from the agent-global tool timeout (``Agent`` retry/timeout config):
this composes at the pipeline seam so an embedder can add a deadline to a
specific pipeline without touching agent configuration.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from typing import Any

from linch.errors import ToolTimeoutError
from linch.tools.pipeline import ToolExecution


def _consume_task_outcome(task: asyncio.Future[Any]) -> None:
    """Retrieve a detached task's exception without changing its outcome."""
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


async def _cancel_bounded(task: asyncio.Future[Any]) -> None:
    """Request cancellation without waiting forever for hostile cleanup.

    Cooperative tasks settle on the first event-loop turn. A body may catch
    that cancellation, so make one further request and then detach it with an
    exception-consuming callback. asyncio cannot forcibly kill a coroutine;
    the deadline wrapper must still return control to its caller.
    """
    task.cancel()
    await asyncio.sleep(0)
    if not task.done():
        task.cancel()
        await asyncio.sleep(0)
    if task.done():
        _consume_task_outcome(task)
    else:
        task.add_done_callback(_consume_task_outcome)


def timeout_wrapper(seconds: float) -> Callable[..., Any]:
    """Build an around-wrapper that fails the tool body after *seconds*.

    Raises :class:`~linch.errors.ToolTimeoutError` when the wrapped body (and
    everything it wraps) does not finish in time.
    """

    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise TypeError("seconds must be a finite positive number")
    normalized_seconds = float(seconds)
    if not math.isfinite(normalized_seconds) or normalized_seconds <= 0:
        raise ValueError("seconds must be a finite positive number")

    async def wrapper(execution: ToolExecution, next: Callable[[], Any]) -> Any:
        # ``asyncio.wait_for`` on Python 3.10 cannot reliably tell its own
        # deadline from an asyncio.TimeoutError raised by the wrapped body.
        # Waiting on a separate task leaves the body's exception untouched and
        # lets only an actually-pending task become ToolTimeoutError.
        task = asyncio.ensure_future(next())
        try:
            await asyncio.wait((task,), timeout=normalized_seconds)
        except BaseException:
            try:
                await _cancel_bounded(task)
            except BaseException:  # noqa: B036 — preserve the outer cancellation/error
                pass
            raise

        if task.done():
            # This preserves an inner TimeoutError rather than mislabeling it
            # as the wrapper's deadline.
            return await task

        try:
            await _cancel_bounded(task)
        except BaseException:  # noqa: B036 — the deadline remains authoritative
            pass
        raise ToolTimeoutError(
            f"Tool {execution.tool_name!r} exceeded pipeline timeout of {normalized_seconds:g}s"
        )

    wrapper.resume_policy_id = "linch.tools.wrappers.timeout"  # type: ignore[attr-defined]
    wrapper.resume_policy_version = "2"  # type: ignore[attr-defined]
    wrapper.resume_policy_config = {  # type: ignore[attr-defined]
        "seconds": normalized_seconds
    }
    return wrapper
