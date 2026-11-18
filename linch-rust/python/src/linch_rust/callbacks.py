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
    "tool_execute",
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
    fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

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

    Python Linch's `retry` and `force_continue` are answers at three points.
    At `before_provider_call`, `{"action": "force_continue", "feedback":
    "..."}` skips the provider call and sends the feedback before another
    turn (bounded by `max_turns` and by `spec.loop_guard`'s
    `max_idle_turns`, 25 turns in a row by default); `"retry"` is refused
    there. At
    `after_provider_call`, `{"action": "retry", "feedback": "..."}` (or
    `"force_continue"`) keeps the response, runs none of its tool calls and
    sends the feedback before another turn. At `stop`, `"force_continue"`
    is `"continue"` (bounded only by `max_turns`), and `"retry"` is
    `before_final_answer`'s retry, honoured once per run call (with
    `spec.verification`, while its `max_retries` lasts, each a
    `VerificationEvent` under the callback's `policy_id`); a ported
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
        timeout: Seconds the server waits for an answer. When absent, the
            gates `pre_tool_use` and `permission_request` wait 30 seconds,
            and every other point has no limit.
        fail_mode: `closed` turns a failure into the point's blocking answer,
            `open` skips the callback; the point's default when absent. A
            callback that guards something (a redaction, a filter) at a point
            that fails open lets the data through when it fails: set
            `closed` where the point allows it.

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


ToolResultWire = dict[str, Any]
ToolNext = Callable[[], Awaitable[ToolResultWire]]
ToolMiddlewareFn = Callable[[dict[str, Any], ToolNext], Awaitable[ToolResultWire]]


def tool_middleware(
    policy_id: str,
    policy_version: int,
    matcher: Sequence[str] | None = None,
    timeout: float | None = None,
    fail_mode: FailMode | None = None,
    park_timeout: float = 600.0,
) -> Callable[[ToolMiddlewareFn], Callback]:
    """Declare a function as a layer of the `tool_execute` waterfall.

    The function is `async def layer(call, next)` and works as Python
    Linch's `tools/execute` listener: `call` is the request (`session_id`,
    `run_id`, `tool_use_id`, `tool_name`, `input`, `attempt`), and awaiting
    `next()` runs the layers after this one and the tool, then returns the
    tool result as a wire result (`{"output": {"value": ...}}` or
    `{"error": {"message": ...}}`). Return that result as it is to keep it,
    or another wire result to replace it. Returning without awaiting
    `next()` vetoes the call: the tool never runs and what you return is the
    result. `next` can be awaited once.

    The server asks the client twice for a layer that awaits `next()`, and
    this decorator parks the coroutine between the two calls, so a layer
    that waits longer than `park_timeout` for the result gets a
    `TimeoutError` from `next()`. The first registered layer is the
    outermost. An exception, a bad result or a timeout is handled by the
    fail mode, `closed` by default: the call fails and the tool does not
    run (or, after it ran, its result is withheld).

    A `tool_cache` hit skips the layers: a call served from the cache never
    reaches this function, so a fail-closed layer does not see it either.

    Args:
        policy_id: Stable id of the policy; with the version it enters the
            run contract.
        policy_version: Bump it whenever the behaviour, matcher, timeout or
            fail mode changes, so a resumed run notices.
        matcher: Tool-name globs (`*`, `?`); evaluated in the server.
        timeout: Seconds the server waits for each of the two calls.
        fail_mode: `closed` (the default) or `open`.
        park_timeout: Seconds the layer may wait for the tool's result.

    Returns:
        A decorator that turns the function into a `Callback`.

    Raises:
        ValueError: A timeout is not positive.
    """
    timeout_ms = None if timeout is None else to_millis(timeout, "timeout")
    to_millis(park_timeout, "park_timeout")

    def wrap(fn: ToolMiddlewareFn) -> Callback:
        parked: dict[tuple[Any, ...], _Parked] = {}

        async def answer(request: dict[str, Any]) -> dict[str, Any]:
            # A token counts per agent and the request does not name the
            # agent, so one decorated layer shared by two agents, or by two
            # connections, sees equal tokens. The run and the call tell
            # them apart: run ids are unique.
            token = (
                request.get("run_id"),
                request.get("tool_use_id"),
                request.get("attempt"),
                request.get("token"),
            )
            if request.get("phase") == "after":
                waiting = parked.pop(token, None)
                if waiting is None:
                    raise RuntimeError("tool_execute: no layer is waiting for this result")
                waiting.result.set_result(request["result"])
                return await waiting.final
            waiting = _Parked(
                result=asyncio.get_running_loop().create_future(),
                first=asyncio.get_running_loop().create_future(),
                final=asyncio.get_running_loop().create_future(),
            )
            parked[token] = waiting
            task = asyncio.ensure_future(_run_layer(fn, request, waiting, park_timeout))
            task.add_done_callback(lambda _: parked.pop(token, None))
            try:
                return await waiting.first
            except BaseException:
                parked.pop(token, None)
                task.cancel()
                raise

        return Callback(
            point="tool_execute",
            policy_id=policy_id,
            policy_version=policy_version,
            matcher=None if matcher is None else tuple(matcher),
            timeout_ms=timeout_ms,
            fail_mode=fail_mode,
            fn=answer,
        )

    return wrap


@dataclass
class _Parked:
    """The three answers of one layer call: to `before`, to `after`, and the result between."""

    result: asyncio.Future[ToolResultWire]
    first: asyncio.Future[dict[str, Any]]
    final: asyncio.Future[dict[str, Any]]


async def _run_layer(
    fn: ToolMiddlewareFn, request: dict[str, Any], waiting: _Parked, park_timeout: float
) -> None:
    called = False

    async def next_() -> ToolResultWire:
        nonlocal called
        if called:
            raise RuntimeError("next() can be awaited once")
        called = True
        waiting.first.set_result({"action": "next", "after": True})
        return await asyncio.wait_for(waiting.result, park_timeout)

    try:
        returned = await fn(request, next_)
        if not called:
            waiting.first.set_result({"action": "veto", "result": returned})
            return
        if returned == waiting.result.result():
            waiting.final.set_result({"action": "continue"})
        else:
            waiting.final.set_result({"action": "replace", "result": returned})
    except BaseException as error:
        for future in (waiting.first, waiting.final):
            if not future.done():
                future.set_exception(error)
                future.exception()  # retrieved here, so an unawaited one stays quiet
        if not isinstance(error, Exception):
            raise


def _threaded(fn: Callable[..., Any]) -> Any:
    @functools.wraps(fn)
    async def run(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    return run
