"""Directed workflows: a Python function directs agent calls the server journals.

`run_workflow(agent, fn, run_id=...)` runs `fn(wf)` in this process. Its
journaled calls (`wf.agent`, `wf.step`, `wf.interrupt`) go to the server,
which keeps the journal (in Postgres, for a durable one): start the same
`run_id` again after a crash and every finished call replays instead of
running again. `fn` itself runs again from the top on a resume, so code
outside the journaled calls must be deterministic or idempotent.

`run_or_resume` is the same idea for one plain run: start it under a chosen
`run_id`, or continue it, or read its result, whichever the store says.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Any, TypeVar, cast

from ._rpc import Connection, JsonObject
from .agent import Agent
from .config import OutputSchema, Principal, ToolChoice
from .errors import LinchError, RunExists, RunLeased, RunTerminal, SessionBusy
from .events import ResultEvent, SuspendedEvent, WorkflowEvent

T = TypeVar("T")
U = TypeVar("U")

_LOG = logging.getLogger(__name__)

# The fan-out branch of the code being run: the branch index at each level,
# outermost first, joined by `.`; empty outside every `parallel`, `settled`
# and `pipeline` branch. Each branch task has its own value.
_BRANCH: contextvars.ContextVar[str] = contextvars.ContextVar("linch_workflow_branch", default="")


class WorkflowSuspended(BaseException):
    """An interrupt has no answer yet: the workflow is parked, not failed.

    It extends `BaseException`, so an `except Exception` in the workflow
    function cannot swallow it. `run_workflow` ends the drive and raises it;
    answer by starting the same `run_id` again with `resume={key: value}`.

    Attributes:
        key: The interrupt's key, the key of the answer.
        payload: What the workflow asked.
    """

    def __init__(self, key: str, payload: Any) -> None:
        super().__init__(f"workflow suspended at interrupt {key!r}")
        self.key = key
        self.payload = payload


class WorkflowTimeoutError(LinchError):
    """A `wf.step` attempt or the whole workflow ran past its time."""


@dataclass(frozen=True)
class Retry:
    """Retries with exponential backoff for a failed `wf.agent` or `wf.step`.

    Only the winning attempt is journaled, so a retried call still replays
    once; a step's function has to be safe to run more than once.

    Attributes:
        max_attempts: Most attempts, the first one included.
        base_delay_ms: Wait before the first retry; each later one waits
            twice as long.
        max_delay_ms: Longest wait between two attempts.
    """

    max_attempts: int
    base_delay_ms: int = 1000
    max_delay_ms: int = 30_000

    def delay(self, failed: int) -> float:
        """Seconds to wait after failed attempt number `failed` (0 for the first)."""
        return min(self.base_delay_ms * 2**failed, self.max_delay_ms) / 1000


@dataclass(frozen=True)
class Budget:
    """A spending cap shared by every agent call of one workflow.

    Attributes:
        max_tokens: Most tokens, all four usage buckets summed.
        max_cost_usd: Most USD, by the agent's pricing.
    """

    max_tokens: int | None = None
    max_cost_usd: float | None = None

    def wire(self) -> JsonObject:
        """The `budget` of `workflow.start`."""
        wire: JsonObject = {}
        if self.max_tokens is not None:
            wire["max_tokens"] = self.max_tokens
        if self.max_cost_usd is not None:
            wire["max_cost_usd"] = self.max_cost_usd
        return wire


@dataclass(frozen=True)
class AgentResult:
    """What one `wf.agent_result` call returned.

    Attributes:
        text: The run's final text; with an output schema, the compact JSON
            of the checked answer.
        structured: The checked answer, with an output schema.
        session_id: The session the run ran in; pass it as a later call's
            `fork` to continue the same conversation.
        tokens: Tokens the run spent.
        cost_usd: The priced cost of the run in USD.
        replayed: Whether the journal answered, so no run was made.
    """

    text: str
    structured: JsonObject | None
    session_id: str
    tokens: int
    cost_usd: float
    replayed: bool


@dataclass(frozen=True)
class StepOutcome:
    """One branch of `wf.settled`: its value, or the error that ended it."""

    value: Any = None
    error: Exception | None = None

    @property
    def ok(self) -> bool:
        """Whether the branch returned a value."""
        return self.error is None


@dataclass(frozen=True)
class WorkflowStatus:
    """A stored workflow, from `workflow_status`.

    Attributes:
        status: `running` (a driver that crashed, or is still driving),
            `awaiting_input`, `completed` or `failed`.
        records: Journal records.
        interrupt_key: The key a workflow awaiting input is parked at.
        interrupt_payload: What it asked.
    """

    status: str
    records: int
    interrupt_key: str | None
    interrupt_payload: Any


class WorkflowContext:
    """The handle a workflow function orchestrates with.

    Only journaled calls (`agent`, `step`, `interrupt`) replay on a resume;
    everything else in the function runs again, so its control flow must not
    depend on anything the journal never saw (randomness, the clock, the
    environment). Put such values inside a step.
    """

    def __init__(
        self,
        conn: Connection,
        run_id: str,
        *,
        max_concurrency: int,
        step_timeout_ms: int | None,
        budget: Budget | None,
        on_event: Callable[[WorkflowEvent], None] | None,
    ) -> None:
        self._conn = conn
        self._run_id = run_id
        self._max_concurrency = max(1, max_concurrency)
        self._step_timeout_ms = step_timeout_ms
        self._on_event = on_event
        self.budget = budget
        self._tokens = 0
        self._usd = 0.0

    @property
    def spent_tokens(self) -> int:
        """Tokens the agent calls of this drive have spent, replayed ones included."""
        return self._tokens

    @property
    def spent_usd(self) -> float:
        """The priced cost of those calls in USD."""
        return self._usd

    async def phase(self, title: str) -> None:
        """Mark a named phase: a progress event for `on_event`, journaled nowhere."""
        self._emit(WorkflowEvent(kind="phase", title=title))

    async def agent(
        self,
        prompt: str,
        *,
        name: str | None = None,
        subagent: str | None = None,
        label: str | None = None,
        key: str | None = None,
        output_schema: OutputSchema | None = None,
        fork: str | AgentResult | None = None,
        timeout_ms: int | None = None,
        retry: Retry | None = None,
    ) -> str:
        """Run the agent on `prompt` as a journaled call and return its final text.

        See `agent_result` for the arguments and the other fields.
        """
        result = await self.agent_result(
            prompt,
            name=name,
            subagent=subagent,
            label=label,
            key=key,
            output_schema=output_schema,
            fork=fork,
            timeout_ms=timeout_ms,
            retry=retry,
        )
        return result.text

    async def agent_result(
        self,
        prompt: str,
        *,
        name: str | None = None,
        subagent: str | None = None,
        label: str | None = None,
        key: str | None = None,
        output_schema: OutputSchema | None = None,
        fork: str | AgentResult | None = None,
        timeout_ms: int | None = None,
        retry: Retry | None = None,
    ) -> AgentResult:
        """Run the agent on `prompt` as a journaled call.

        The server runs it as a child run in a session of its own and
        answers when it ends, or from the journal with no run when an
        earlier drive finished the same call. A call is identified by its
        name, prompt, options and how many identical calls came before it,
        so editing a prompt makes only that call run again.

        Args:
            prompt: The prompt of the run.
            name: The call's name in its journal key and events.
            subagent: Run this subagent definition of the agent (its tools
                and prompt) instead of the agent itself.
            label: The title of the call's events; not part of the key.
            key: A caller-owned identity in place of the prompt and options:
                the call replays while it stays the same.
            output_schema: Check the answer against this schema; `text` is
                then its compact JSON and `structured` the object.
            fork: A session (or the result of an earlier call, whose
                session is used) to continue: the run starts on a copy of
                its durable log, so it sees the same conversation.
            timeout_ms: Limit on each attempt, which aborts the run; `None`
                uses the workflow's `step_timeout_ms`, `0` none.
            retry: Retries after a failed or timed out attempt.

        Returns:
            The call's result.

        Raises:
            WorkflowCallFailed: The run failed, was aborted, timed out or
                waits for an approval.
            WorkflowBudgetExceeded: The workflow's budget is spent.
            LimitReached: A cap of the tenant's workflow allowlists.
        """
        params: JsonObject = {"prompt": prompt}
        if name is not None:
            params["name"] = name
        if subagent is not None:
            params["subagent"] = subagent
        if label is not None:
            params["label"] = label
        if key is not None:
            params["key"] = key
        if output_schema is not None:
            params["output_schema"] = dict(output_schema)
        if fork is not None:
            params["fork"] = fork.session_id if isinstance(fork, AgentResult) else fork
        timeout = timeout_ms if timeout_ms is not None else self._step_timeout_ms
        if timeout:
            params["timeout_ms"] = timeout
        if retry is not None:
            params["retry"] = {
                "max_attempts": retry.max_attempts,
                "base_delay_ms": retry.base_delay_ms,
                "max_delay_ms": retry.max_delay_ms,
            }
        call_id = uuid.uuid4().hex
        params["call_id"] = call_id
        answer = await self._call_cancellable("workflow.agent", params, call_id)
        structured = answer.get("structured")
        result = AgentResult(
            text=str(answer["text"]),
            structured=cast("JsonObject | None", structured),
            session_id=str(answer["session_id"]),
            tokens=int(answer["tokens"]),
            cost_usd=float(answer["cost_usd"]),
            replayed=bool(answer["replayed"]),
        )
        self._tokens += result.tokens
        self._usd += result.cost_usd
        return result

    async def step(
        self,
        name: str,
        fn: Callable[[], Any],
        *,
        key: Any = None,
        timeout_ms: int | None = None,
        retry: Retry | None = None,
    ) -> Any:
        """Run `fn` as a journaled step and return its value.

        This is how code with side effects becomes a node a resume replays
        instead of running again. The value goes through JSON on every
        path, so a replayed value equals the live one, and it is durable in
        the journal before this returns. A process that crashes after `fn`
        ran but before the record landed runs `fn` again on the resume, so
        an irreversible step (sign, send, pay) must be idempotent: put the
        idempotency key in `key` and pass it to the system you call.

        Args:
            name: The step's durable identity; renaming it makes the record it had
                miss.
            fn: A zero-argument callable, sync or async; it runs in this
                process.
            key: JSON that identifies the step's inputs, so one name over
                different inputs is journaled separately.
            timeout_ms: Limit on each attempt; `None` uses the workflow's
                `step_timeout_ms`, `0` none.
            retry: Retries after a failed or timed out attempt.

        Returns:
            `fn`'s value, as JSON.

        Raises:
            WorkflowTimeoutError: An attempt ran past its limit.
            TypeError: The value is not JSON.
        """
        params: JsonObject = {"name": name}
        if key is not None:
            params["key"] = key
        begun = await self._call("workflow.step.begin", params)
        if begun["replayed"]:
            return begun.get("value")
        timeout = timeout_ms if timeout_ms is not None else self._step_timeout_ms
        value = await _attempts(name, fn, timeout, retry)
        # The value a replay would give: encoding refuses NaN and non-JSON.
        value = json.loads(json.dumps(value, allow_nan=False))
        await self._call(
            "workflow.step.complete", {"ticket": begun["ticket"], "value": value}, branch=False
        )
        return value

    async def interrupt(self, key: str, payload: Any = None) -> Any:
        """Park the workflow until someone answers `key`.

        The first time through there is no answer: the workflow is stored
        as awaiting input (not failed) with `payload`, and `WorkflowSuspended`
        ends `run_workflow`. Start the same `run_id` again with
        `resume={key: answer}` and this call returns the answer; it is
        journaled, so later drives replay it. An answer is used once, so
        interrupts in a loop need distinct keys. An answer applies only to
        the question that was asked: if `payload` differs from the stored
        question's (a draft edited since), the call suspends again with the
        new payload and the answer is dropped (an `interrupt_stale` event).
        Answer the interrupt that `workflow_status` names (`interrupt_key`).

        Raises:
            WorkflowSuspended: There is no answer yet.
        """
        asked: JsonObject = {"key": key, "payload": payload}
        answer = await self._call("workflow.interrupt", asked)
        if answer["suspended"]:
            raise WorkflowSuspended(key, payload)
        return answer.get("value")

    async def parallel(self, thunks: Sequence[Callable[[], Awaitable[T]]]) -> list[T]:
        """Run `thunks` concurrently, at most `max_concurrency` at once.

        Results keep input order. The first branch that fails cancels its
        siblings, whose calls in flight are aborted on the server (a
        cancelled call journals nothing and runs again on the resume), and
        raises. Cancelling the fan-out does the same.
        """
        slots = asyncio.Semaphore(self._max_concurrency)
        tasks = [
            asyncio.ensure_future(self._branch(index, thunk, slots))
            for index, thunk in enumerate(thunks)
        ]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def settled(self, thunks: Sequence[Callable[[], Awaitable[T]]]) -> list[StepOutcome]:
        """Like `parallel`, but a failing branch leaves its siblings running.

        Returns:
            One `StepOutcome` per branch, in input order. A suspend is
            control flow, not an outcome: once every branch has finished
            the first `WorkflowSuspended` is raised instead of the list.
        """
        slots = asyncio.Semaphore(self._max_concurrency)
        tasks = [
            asyncio.ensure_future(self._branch(index, thunk, slots))
            for index, thunk in enumerate(thunks)
        ]
        try:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        results: list[StepOutcome] = []
        for outcome in outcomes:
            if isinstance(outcome, WorkflowSuspended):
                raise outcome
            if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, Exception):
                results.append(StepOutcome(error=outcome))
            else:
                results.append(StepOutcome(value=outcome))
        return results

    async def pipeline(
        self, items: Sequence[T], *stages: Callable[[Any], Awaitable[Any]]
    ) -> list[Any]:
        """Run each item through every stage, one stage after the other.

        Items run concurrently and independently, with no barrier between
        stages: a slow item never holds up a fast one. Each stage gets the
        previous stage's value. Failures are as in `parallel`.
        """

        def chain(item: Any) -> Callable[[], Awaitable[Any]]:
            async def run() -> Any:
                value = item
                for stage in stages:
                    value = await stage(value)
                return value

            return run

        return await self.parallel([chain(item) for item in items])

    async def _branch(
        self,
        index: int,
        thunk: Callable[[], Awaitable[T]],
        slots: asyncio.Semaphore,
    ) -> T:
        parent = _BRANCH.get()
        _BRANCH.set(f"{parent}.{index}" if parent else str(index))
        async with slots:
            return await thunk()

    async def _call(self, method: str, params: JsonObject, *, branch: bool = True) -> JsonObject:
        wire = {"run_id": self._run_id, **params}
        if branch and (path := _BRANCH.get()):
            wire["branch"] = path
        return await self._conn.request(method, wire)

    async def _call_cancellable(self, method: str, params: JsonObject, call_id: str) -> JsonObject:
        """Send a call whose server work stops when this task is cancelled.

        Cancelling the awaiting task asks the server to abort the call and
        waits until it has ended, so a cancelled fan-out has stopped
        spending when it raises.
        """
        sent = asyncio.ensure_future(self._call(method, params))
        try:
            return await asyncio.shield(sent)
        except asyncio.CancelledError:
            with contextlib.suppress(LinchError):
                await asyncio.shield(
                    self._conn.request(
                        "workflow.cancel", {"run_id": self._run_id, "call_id": call_id}
                    )
                )
            with contextlib.suppress(LinchError):
                await sent
            raise

    def _emit(self, event: WorkflowEvent) -> None:
        if self._on_event is not None:
            self._on_event(event)


async def _attempts(
    name: str, fn: Callable[[], Any], timeout_ms: int | None, retry: Retry | None
) -> Any:
    """Run `fn` until it succeeds or runs out of attempts, each under its timeout."""
    attempts = max(1, retry.max_attempts) if retry is not None else 1
    failed = 0
    while True:
        try:
            if timeout_ms:
                async with asyncio.timeout(timeout_ms / 1000):
                    return await _call_fn(fn)
            return await _call_fn(fn)
        except TimeoutError as error:
            outcome: Exception = WorkflowTimeoutError(
                f"step {name} timed out after {timeout_ms} ms"
            )
            outcome.__cause__ = error
        except Exception as error:
            outcome = error
        if retry is None or failed + 1 >= attempts:
            raise outcome
        await asyncio.sleep(retry.delay(failed))
        failed += 1


async def _call_fn(fn: Callable[[], Any]) -> Any:
    value = fn()
    if inspect.isawaitable(value):
        return await value
    return value


async def run_workflow(
    agent: Agent,
    fn: Callable[[WorkflowContext], Awaitable[T]],
    *,
    run_id: str,
    budget: Budget | None = None,
    max_concurrency: int = 4,
    step_timeout_ms: int | None = None,
    deadline_ms: int | None = None,
    resume: Mapping[str, Any] | None = None,
    on_event: Callable[[WorkflowEvent], None] | None = None,
) -> T:
    """Run `fn(wf)` as a directed workflow named `run_id`, and return its value.

    The server journals every `wf.agent`, `wf.step` and `wf.interrupt` call
    under `run_id`: start the same id again (after a crash, or with an
    answer) and each call it already finished replays, while `fn` itself
    runs again from the top. A workflow has one driver at a time; with
    memory stores the journal lives as long as the connection.

    Args:
        agent: The opened agent whose runs the workflow's calls make and
            whose stores hold the journal.
        fn: The workflow function; it gets a `WorkflowContext`.
        run_id: The workflow's id, unique in the agent's namespace.
        budget: Caps what the workflow's agent calls spend, replays counted.
        max_concurrency: Most branches one `parallel` or `settled` call
            runs at once; each nesting level has its own limit. A tenant's
            `max_fan_out` lowers it.
        step_timeout_ms: Default limit on each attempt of a step or an agent
            call; a call's own `timeout_ms` wins.
        deadline_ms: Limit on the whole function; past it the function is
            cancelled, the calls in flight abort, and the finished calls
            stay journaled for a resume.
        resume: Answers to interrupts an earlier drive left pending, by key.
        on_event: Called with each `WorkflowEvent` (phase, agent and step
            start, end and replay, interrupt), in order.

    Returns:
        What `fn` returned.

    Raises:
        WorkflowSuspended: An interrupt has no answer: the workflow is
            stored as awaiting input.
        WorkflowTimeoutError: `deadline_ms` passed.
        WorkflowHeld: Another client drives the workflow.
        NotAllowed: The tenant's allowlists give it no workflows.
    """
    conn = agent._conn  # pyright: ignore[reportPrivateUsage]
    params: JsonObject = {"agent": agent.handle, "run_id": run_id}
    if budget is not None:
        params["budget"] = budget.wire()
    if resume:
        params["resume"] = dict(resume)
    started = await conn.request("workflow.start", params)
    limits = cast("JsonObject", started.get("limits") or {})
    fan_out = limits.get("max_fan_out")
    concurrency = min(max_concurrency, int(fan_out)) if fan_out else max_concurrency
    if on_event is not None:
        sink = on_event
        conn.watch_workflow(run_id, lambda event: _deliver(sink, event))
    wf = WorkflowContext(
        conn,
        run_id,
        max_concurrency=concurrency,
        step_timeout_ms=step_timeout_ms,
        budget=budget,
        on_event=on_event,
    )
    ok = False
    try:
        if deadline_ms:
            try:
                async with asyncio.timeout(deadline_ms / 1000):
                    value = await fn(wf)
            except TimeoutError as error:
                raise WorkflowTimeoutError(
                    f"workflow {run_id!r} ran past its deadline of {deadline_ms} ms"
                ) from error
        else:
            value = await fn(wf)
        ok = True
        return value
    finally:
        # Shielded: a cancelled drive still tells the server it is done, so
        # the workflow is free for the next one.
        try:
            await asyncio.shield(
                _finish(conn, run_id, ok),
            )
        finally:
            conn.unwatch_workflow(run_id)


async def _finish(conn: Connection, run_id: str, ok: bool) -> None:
    # A closed connection already ended the drive on the server.
    with contextlib.suppress(LinchError):
        await conn.request("workflow.finish", {"run_id": run_id, "ok": ok})


def _deliver(sink: Callable[[WorkflowEvent], None], raw: JsonObject) -> None:
    fields = WorkflowEvent.__dataclass_fields__
    try:
        sink(WorkflowEvent(**{key: value for key, value in raw.items() if key in fields}))
    except Exception:
        _LOG.exception("on_event raised")


async def workflow_status(agent: Agent, run_id: str) -> WorkflowStatus:
    """Read a stored workflow without driving it.

    Raises:
        RunNotFound: The agent's namespace has no workflow with that id.
    """
    conn = agent._conn  # pyright: ignore[reportPrivateUsage]
    answer = await conn.request("workflow.status", {"agent": agent.handle, "run_id": run_id})
    interrupt = cast("JsonObject | None", answer.get("interrupt"))
    return WorkflowStatus(
        status=str(answer["status"]),
        records=int(answer["records"]),
        interrupt_key=str(interrupt["key"]) if interrupt else None,
        interrupt_payload=interrupt.get("payload") if interrupt else None,
    )


async def run_or_resume(
    agent: Agent,
    run_id: str,
    prompt: str,
    *,
    session_id: str | None = None,
    principal: Principal | None = None,
    trace_id: str | None = None,
    output_schema: OutputSchema | None = None,
    tool_choice: ToolChoice | None = None,
    wait: float = 60.0,
) -> ResultEvent | SuspendedEvent:
    """Make sure the run `run_id` has run, whatever state it is in, and return how it stopped.

    `Agent.run` with a `run_id` that is already stored ends with `RunExists`
    (-32023). This helper makes the call idempotent, so a retried job, a
    scheduler node or an orchestrator task (LangGraph, Prefect) can call it
    again after a crash:

    - not stored: the run starts;
    - stored and finished: its result is returned, with no model call;
    - stored and waiting for an approval or an answer: the `SuspendedEvent`
      is returned (approve or answer it, then call this again);
    - stored and unfinished (the process that drove it died): the run
      continues from its journal, which never pays a model call twice;
    - stored and driven by another process now: this waits, up to `wait`
      seconds, for its lease to end, then continues it.

    Args:
        agent: The opened agent; with memory stores, a run lives in its connection.
        run_id: The run's id, chosen by the caller and stable across retries.
        prompt: The prompt, used only when the run is new.
        session_id: The session; `run_id` when absent.
        principal: Who the run acts for, as in `Agent.run`.
        trace_id: The host's trace id, as in `Agent.run`.
        output_schema: The run's output schema, as in `Agent.run`.
        tool_choice: The run's tool choice, as in `Agent.run`.
        wait: Seconds to wait for another process's lease.

    Returns:
        The `ResultEvent` of a finished run, or the `SuspendedEvent` of one
        waiting for an approval or an answer.

    Raises:
        RunLeased: Another process still drives the run after `wait` seconds.
        RunError: The run could not continue durably.
    """
    run = await agent.run(
        session_id or run_id,
        prompt,
        run_id=run_id,
        principal=principal,
        trace_id=trace_id,
        output_schema=output_schema,
        tool_choice=tool_choice,
    )
    try:
        return await run.result()
    except RunExists:
        pass
    except SessionBusy as busy:
        # The session's live run is not ours: nothing to continue here.
        if busy.run_id != run_id:
            raise
    deadline = monotonic() + wait
    while True:
        stored = await _stored_outcome(agent, run_id)
        if stored is not None:
            return stored
        try:
            continued = await agent.resume(run_id, principal=principal)
            return await continued.result()
        except RunLeased as leased:
            left = deadline - monotonic()
            if left <= 0:
                raise
            await asyncio.sleep(min(leased.expires_in or 0.2, left))
        except RunTerminal:
            # It finished between the read and the resume: read it again.
            continue


async def _stored_outcome(agent: Agent, run_id: str) -> ResultEvent | SuspendedEvent | None:
    """How a stored run ended or stopped; `None` while it is unfinished."""
    ended: ResultEvent | SuspendedEvent | None = None
    for envelope in await agent.events(run_id):
        event = envelope.event
        if isinstance(event, ResultEvent):
            return event
        if isinstance(event, SuspendedEvent):
            ended = event
        elif ended is not None:
            # A later event, such as a resume's `init`, reopens the run.
            ended = None
    return ended


__all__ = [
    "AgentResult",
    "Budget",
    "Retry",
    "StepOutcome",
    "WorkflowContext",
    "WorkflowStatus",
    "WorkflowSuspended",
    "WorkflowTimeoutError",
    "run_or_resume",
    "run_workflow",
    "workflow_status",
]
