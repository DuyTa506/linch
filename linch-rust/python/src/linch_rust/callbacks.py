"""Intercept callbacks: async Python functions the server asks at a hook point."""

from __future__ import annotations

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
]
FailMode = Literal["open", "closed"]
CallbackFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


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
    """Declare an async function as an intercept callback at `point`.

    The function takes the `hook.call` request (a dict with `session_id`,
    `run_id` and the point's fields) and returns the point's answer dict,
    for example `{"action": "block", "reason": "..."}` at `pre_tool_use`;
    the `linch-rpc` crate docs list each point's request and answers. An
    exception, a bad answer or a timeout is handled by the fail mode.

    Args:
        point: The hook point it intercepts.
        policy_id: Stable id of the policy; with the version it enters the
            run contract.
        policy_version: Bump it whenever the behaviour, matcher, timeout or
            fail mode changes, so a resumed run notices.
        matcher: Tool-name globs (`*`, `?`), only at the three tool points;
            evaluated in the server, so a miss never crosses the connection.
        timeout: Seconds the server waits for an answer; 30 when absent.
        fail_mode: `closed` turns a failure into the point's blocking answer,
            `open` skips the callback; the point's default when absent.

    Returns:
        A decorator that turns the function into a `Callback`.

    Raises:
        TypeError: The function is not `async`.
        ValueError: The timeout is not positive.
    """
    timeout_ms = None if timeout is None else to_millis(timeout, "timeout")

    def wrap(fn: CallbackFn) -> Callback:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"callback {policy_id!r} must be an async function")
        return Callback(
            point=point,
            policy_id=policy_id,
            policy_version=policy_version,
            matcher=None if matcher is None else tuple(matcher),
            timeout_ms=timeout_ms,
            fail_mode=fail_mode,
            fn=fn,
        )

    return wrap
