"""Intercept callbacks: async Python functions the server asks at a hook point."""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .tools import to_millis

Point = Literal[
    "user_prompt_submit",
    "context_build",
    "pre_tool_use",
    "permission_request",
    "post_tool_use",
    "stop",
    "post_turn",
    "agent_start",
    "agent_stop",
    "turn_start",
    "before_provider_call",
    "provider_call_start",
    "provider_call_stop",
    "after_provider_call",
    "tool_use_start",
    "tool_use_stop",
    "post_tool_use_failure",
    "pre_compact",
    "post_compact",
    "subagent_start",
    "subagent_stop",
    "event_emit",
]
FailMode = Literal["open", "closed"]
CallbackFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


@dataclass(frozen=True)
class Callback:
    """A callback declaration and the function that answers it; made by `callback`."""

    point: Point
    policy_id: str
    policy_version: int
    matcher: tuple[str, ...] | None
    timeout_ms: int | None
    fail_mode: FailMode | None
    fn: CallbackFn

    def spec(self) -> dict[str, Any]:
        """The declaration as `agent.open` takes it in `callbacks`."""
        spec: dict[str, Any] = {
            "point": self.point,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
        }
        if self.matcher is not None:
            spec["matcher"] = list(self.matcher)
        if self.timeout_ms is not None:
            spec["timeout_ms"] = self.timeout_ms
        if self.fail_mode is not None:
            spec["fail_mode"] = self.fail_mode
        return spec


def callback(
    point: Point,
    policy_id: str,
    policy_version: int,
    matcher: Sequence[str] | None = None,
    timeout: float | None = None,
    fail_mode: FailMode | None = None,
) -> Callable[[CallbackFn], Callback]:
    """Declare a function as an intercept callback at `point`.

    The function takes the `hook.call` request (a dict with `session_id`,
    `run_id` and the point's fields) and returns the point's answer dict,
    for example `{"action": "block", "reason": "..."}` at `pre_tool_use`;
    the `linch-rpc` crate docs list each point's request and answers. A
    plain function runs in a worker thread, so it never blocks the event
    loop. An
    exception, a bad answer or a timeout is handled by the fail mode.

    Python Linch's `retry` and `force_continue` are answers at two points.
    At `after_provider_call`, `{"action": "retry", "feedback": "..."}` (or
    `"force_continue"`) keeps the response, runs none of its tool calls and
    sends the feedback before another turn. At `stop`, `"force_continue"`
    is `"continue"` (bounded only by `max_turns`), and `"retry"` is
    `before_final_answer`'s retry, honoured once per run call; a ported
    `before_final_answer` hook that answered `force_continue` answers
    `"retry"` here. Every other point refuses both as a bad answer.

    With an output schema, `stop` runs only on an answer that satisfies it,
    and its request has `structured_output`, the parsed answer. A
    `"rewrite"` is checked against the schema again; one that fails it
    fails the run.

    Args:
        point: The hook point it intercepts.
        policy_id: Stable id of the policy; with the version it enters the
            run contract.
        policy_version: Bump it whenever the behaviour, matcher, timeout or
            fail mode changes, so a resumed run notices.
        matcher: Tool-name globs (`*`, `?`), only at the three tool points;
            evaluated in the server, so a miss never crosses the connection.
        timeout: Seconds the server waits for an answer; no limit when absent.
        fail_mode: `closed` turns a failure into the point's blocking answer,
            `open` skips the callback; the point's default when absent.

    Returns:
        A decorator that turns the function into a `Callback`.

    Raises:
        ValueError: The timeout is not positive.
    """
    timeout_ms = None if timeout is None else to_millis(timeout, "timeout")

    def wrap(fn: CallbackFn) -> Callback:
        return Callback(
            point=point,
            policy_id=policy_id,
            policy_version=policy_version,
            matcher=None if matcher is None else tuple(matcher),
            timeout_ms=timeout_ms,
            fail_mode=fail_mode,
            fn=fn if inspect.iscoroutinefunction(fn) else _threaded(fn),
        )

    return wrap


def _threaded(fn: Callable[..., Any]) -> Any:
    @functools.wraps(fn)
    async def run(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    return run
