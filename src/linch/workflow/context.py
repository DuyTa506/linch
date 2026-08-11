"""WorkflowContext — the ``wf`` object handed to workflow functions."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from contextvars import ContextVar
from typing import Any, cast
from uuid import uuid4

from ..abort import throw_if_aborted
from ..errors import (
    AbortError,
    ConfigError,
    WorkflowError,
    WorkflowSuspended,
    WorkflowTimeoutError,
)
from ..events import Event, WorkflowEvent
from ..providers.retry import RetryOptions, with_retry
from ..session import RunOptions
from .journal import WorkflowJournal, WorkflowJournalRecord, call_key, interrupt_key, step_key

# Which WorkflowContexts the current task is already inside a ``parallel``
# branch of.  The ContextVar object is module-level, but its *value* is
# per-task-context (asyncio copies the context when a Task is created) and is
# only ever replaced, never mutated — so concurrent agents cannot observe each
# other through it.  Markers are per-instance ``object()`` identities.
_PARALLEL_BRANCH: ContextVar[frozenset[object]] = ContextVar("linch_wf_parallel_branch")

# The extension_state namespace a workflow's journal snapshot lives under.
SNAPSHOT_NAMESPACE = "linch.workflow"

# Past this, re-serializing the journal into every Nth checkpoint costs more
# than the one-shot event-log fold it saves, so the snapshot is skipped and the
# event log stays the source of truth.
_SNAPSHOT_MAX_BYTES = 256 * 1024


@dataclasses.dataclass(slots=True)
class StepOutcome:
    """One branch's result from :meth:`WorkflowContext.settled`."""

    ok: bool
    value: Any = None
    error: BaseException | None = None


def _workflow_retry_on(exc: Exception) -> bool:
    """Which ``wf.agent`` / ``wf.step`` failures a ``retry=`` policy retries.

    Everything except a ``ConfigError`` — an unknown subagent name or a
    non-JSON-serializable step value fails identically on every attempt, so
    retrying it only burns the budget. (``AbortError`` is already excluded by
    ``with_retry`` itself.)
    """
    return not isinstance(exc, ConfigError)


class WorkflowContext:
    """Deterministic orchestration primitives for a workflow function.

    The workflow function must be deterministic (no random or time-based
    branching) for resume to replay the unchanged ``wf.agent`` prefix.
    """

    def __init__(
        self,
        agent: Any,
        host_session: Any,
        *,
        journal: WorkflowJournal | None = None,
        budget: Any = None,
        max_concurrency: int = 4,
        max_agent_concurrency: int = 0,
        on_event: Callable[[Event], None] | None = None,
        store: Any = None,
        run_id: str | None = None,
        step_timeout_ms: float | None = None,
        signal: Any = None,
        resume: dict[str, Any] | None = None,
        workflow_name: str = "workflow",
        journal_snapshot_every: int = 0,
    ) -> None:
        self._agent = agent
        self._host_session = host_session
        self._journal = journal or WorkflowJournal()
        self.budget = budget
        self._max_concurrency = max(1, max_concurrency)
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._branch_marker = object()
        self._max_agent_concurrency = max_agent_concurrency
        self._agent_gate: asyncio.Semaphore | None = None
        self._on_event = on_event
        self._store = store
        self._run_id = run_id
        self._step_timeout_ms = step_timeout_ms
        self._signal = signal
        # Copied and consumed per key: one answer resolves one interrupt, so a
        # later interrupt reusing the key suspends again instead of silently
        # reusing a stale decision.
        self._resume = dict(resume or {})
        self._workflow_name = workflow_name
        self._journal_snapshot_every = journal_snapshot_every
        self._records_since_snapshot = 0
        self._last_event_seq = 0
        self._snapshot: dict[str, Any] | None = None

    # ── event plumbing ───────────────────────────────────────────────────

    def _emit_sync(self, event: Event) -> None:
        """Forward an event to the host callback (child SubagentEvents land here)."""
        if self._on_event is not None:
            self._on_event(event)

    async def _emit(self, event: Event) -> None:
        """Forward to the host callback and persist when the run is durable."""
        self._emit_sync(event)
        if self._store is not None and self._run_id is not None:
            seq = await self._store.append_event(self._run_id, event)
            # Watermark for the journal snapshot; parallel branches append
            # concurrently, so take the highest seq seen rather than the last.
            if isinstance(seq, int) and seq > self._last_event_seq:
                self._last_event_seq = seq

    # ── journal snapshot (opt-in resume accelerator) ─────────────────────

    def extension_state(self) -> dict[str, Any]:
        """The ``RunCheckpoint.extension_state`` this workflow wants persisted.

        Empty unless journal snapshots are enabled and one has been taken, so
        a checkpoint written with snapshots off is byte-identical to before
        the feature existed.
        """
        return {SNAPSHOT_NAMESPACE: self._snapshot} if self._snapshot is not None else {}

    async def _maybe_snapshot(self) -> None:
        """Checkpoint the journal every ``journal_snapshot_every`` records.

        A resume seeds its journal from the snapshot and folds only the events
        appended after ``after_seq``, instead of the whole log. A record is
        always journaled before its ``*_end`` event is appended, so a record
        the snapshot missed is guaranteed to be in that tail.
        """
        if self._journal_snapshot_every <= 0 or self._store is None or self._run_id is None:
            return
        self._records_since_snapshot += 1
        if self._records_since_snapshot < self._journal_snapshot_every:
            return
        self._records_since_snapshot = 0

        snapshot = {"after_seq": self._last_event_seq, "records": self._journal.snapshot()}
        if len(json.dumps(snapshot, separators=(",", ":"))) > _SNAPSHOT_MAX_BYTES:
            return
        self._snapshot = snapshot
        checkpoint = workflow_checkpoint(self._workflow_name, "workflow_running")
        checkpoint.extension_state.update(self.extension_state())
        await self._store.save_checkpoint(self._run_id, checkpoint)

    # ── primitives ───────────────────────────────────────────────────────

    async def phase(self, title: str) -> None:
        """Mark a named phase (progress grouping for observers)."""
        await self._emit(WorkflowEvent(kind="phase", title=title))

    async def agent(
        self,
        prompt: str,
        *,
        name: str | None = None,
        label: str | None = None,
        tools: list[str] | None = None,
        run_options: RunOptions | None = None,
        output_schema: Any = None,
        final_tool_name: str | None = None,
        fork: bool = False,
        isolation: Any = None,
        isolation_keep: bool = False,
        timeout_ms: float | None = None,
        retry: RetryOptions | None = None,
    ) -> str:
        """Run a subagent and return its final text.

        Results are journaled; on resume an unchanged call returns its cached
        result without a provider call.

        Args:
            prompt: The prompt handed to the subagent.
            name: Subagent definition to select from the agent's registry;
                None uses the built-in general-purpose subagent.
            label: Display name for progress/events; defaults to name or "agent".
            tools: Tool names available to the subagent; None uses its default set.
            run_options: Run options forwarded to the subagent call.
            output_schema: Structured-output schema, merged into run_options.
            final_tool_name: Final-tool name override, merged into run_options.
            fork: Whether to run as a continuation of the workflow host's context
                (shared conversation prefix, system blocks, tools, read-file
                tracker) so a caching provider reuses the cached prefix — a cost
                win for fan-outs over a large shared context. False isolates
                each subagent.
            isolation: An IsolationBackend that runs the subagent in its own
                acquired working directory, so parallel branches editing the
                same relative path don't collide.
            isolation_keep: Whether to preserve the isolated working directory
                after the branch finishes (e.g. to merge it); only meaningful
                with isolation set.
            timeout_ms: Wall-clock budget for the child run; ``None`` uses the
                workflow's ``step_timeout_ms`` default and ``0`` opts out of it.
            retry: Re-run the child on failure with exponential backoff. Only
                the winning attempt is journaled.

        Returns:
            The subagent's final text.

        Raises:
            WorkflowError: If the child run failed or was aborted.
            WorkflowTimeoutError: If the child exceeded its timeout.
        """
        from ..subagents.default_agent import DEFAULT_AGENT
        from ..subagents.runner import RunSubagentArgs, result_text_for_caller, run_subagent

        definition = None
        if name is None:
            definition = DEFAULT_AGENT
        else:
            registry = getattr(self._agent, "subagent_registry", None)
            if registry is not None:
                definition = registry.get(name)
            if definition is None:
                raise ConfigError(f"unknown subagent type for wf.agent(): {name!r}")

        subagent_type = definition.frontmatter.name
        effective_run_options = _merge_run_options(
            run_options,
            output_schema=output_schema,
            final_tool_name=final_tool_name,
        )
        effective_tools_filter = tools if tools is not None else definition.frontmatter.tools
        if self._journal.fingerprint_version >= 2:
            options_fingerprint = _call_options_fingerprint(
                tools=effective_tools_filter,
                run_options=effective_run_options,
            )
        else:
            # This run's journal predates the tools-aware fingerprint (version 1,
            # see CURRENT_FINGERPRINT_VERSION); keep computing keys the original
            # way for this run's whole lifetime so its existing call_keys keep
            # matching and resume replays instead of silently re-executing.
            options_fingerprint = _run_options_fingerprint(effective_run_options)
        key = call_key(subagent_type, prompt, options_fingerprint)
        display_name = label or name or "agent"

        async def run() -> WorkflowJournalRecord:
            args = RunSubagentArgs(
                parent_session=self._host_session,
                parent_agent=self._agent,
                definition=definition,
                prompt=prompt,
                display_name=display_name,
                subagent_run_id=f"wf_{uuid4().hex[:8]}",
                tools_filter=tools,
                run_options=effective_run_options,
                signal=self._signal,
                emit=self._emit_sync,
                fork=fork,
                isolation=isolation,
                isolation_keep=isolation_keep,
            )
            # Gated around the live call only — a replayed prefix reaches this
            # closure never, so it can never queue behind a provider slot.
            async with self._agent_backpressure():
                result = await run_subagent(args)
            if result.errored:
                error = result.error or {"name": "WorkflowError", "message": "subagent failed"}
                raise WorkflowError(
                    f"wf.agent({display_name!r}) failed: "
                    f"{error.get('name', 'Error')}: {error.get('message', '')}",
                    error=error,
                )
            if result.aborted:
                raise AbortError(f"wf.agent({display_name!r}) was aborted")
            return WorkflowJournalRecord(
                result_text=result_text_for_caller(result),
                structured_output=result.structured_output,
                structured_error=result.structured_error,
            )

        record = await self._journaled_call(
            key=key,
            kinds=("agent_start", "agent_end", "agent_replayed"),
            title=display_name,
            label=f"wf.agent({display_name!r})",
            run=run,
            subagent_type=subagent_type,
            timeout_ms=timeout_ms,
            retry=retry,
        )
        return record.result_text

    async def step(
        self,
        name: str,
        fn: Callable[[], Any],
        *,
        key: Any = None,
        timeout_ms: float | None = None,
        retry: RetryOptions | None = None,
    ) -> Any:
        """Run *fn* as a journaled step and return its value.

        This is how deterministic code, a tool call, or any other side-effecting
        work becomes a replayable node: the value is journaled, so a resumed run
        returns it without executing *fn* a second time.

        Args:
            name: The step's durable identity in the journal. Renaming it
                invalidates that step (and everything after it) on resume.
            fn: A zero-argument callable, sync or async. A sync *fn* must not
                block the event loop.
            key: Optional fingerprint of the step's inputs, so the same *name*
                over different inputs journals separately. Without it, repeated
                calls are told apart only by their order.
            timeout_ms: Wall-clock budget for *fn*; ``None`` uses the workflow's
                ``step_timeout_ms`` default and ``0`` opts out of it.
            retry: Re-run *fn* on failure with exponential backoff. Only the
                winning attempt is journaled, so *fn* must be safe to retry.

        Returns:
            Whatever *fn* returned, round-tripped through JSON.

        Raises:
            ConfigError: If *fn*'s return value is not JSON-serializable — it
                could not be journaled, so it could never replay.
            WorkflowTimeoutError: If *fn* exceeded its timeout.
        """
        key_fingerprint = (
            ""
            if key is None
            else json.dumps(_fingerprint_value(key), sort_keys=True, separators=(",", ":"))
        )

        async def run() -> WorkflowJournalRecord:
            value = fn()
            if inspect.isawaitable(value):
                value = await value
            return WorkflowJournalRecord(
                result_text=_encode_json_value(f"wf.step({name!r})", "return value", value),
                record_kind="step",
            )

        record = await self._journaled_call(
            key=step_key(name, key_fingerprint),
            kinds=("step_start", "step_end", "step_replayed"),
            title=name,
            label=f"wf.step({name!r})",
            run=run,
            timeout_ms=timeout_ms,
            retry=retry,
        )
        return json.loads(record.result_text)

    async def interrupt(self, key: str, payload: Any = None) -> Any:
        """Pause the workflow until someone supplies an answer for *key*.

        The durable human-in-the-loop primitive. On the first pass there is no
        answer, so an ``interrupt_requested`` event is emitted (carrying
        *payload* for whoever decides) and :class:`WorkflowSuspended` is raised
        — the run is marked ``"suspended"``, not failed. Re-invoke
        ``run_workflow`` with the same ``run_id`` and ``resume={key: value}``
        and this call returns *value* instead, journaled like any other node so
        every later resume replays it.

        Args:
            key: The interrupt's durable identity, and the key the answer is
                supplied under. Interrupts inside a loop need distinct keys.
            payload: JSON-serializable context for the decider (a diff, a plan,
                a set of options). It never affects replay.

        Returns:
            The supplied answer, round-tripped through JSON.

        Raises:
            WorkflowSuspended: If no answer is available yet.
            ConfigError: If *payload* or the answer is not JSON-serializable.

        Note:
            Interrupting inside a ``wf.parallel`` branch cancels the sibling
            branches, so a fan-out cannot collect several answers at once. Ask
            for the decisions sequentially, or use ``wf.settled``.
        """
        ikey = interrupt_key(key)
        occurrence = self._journal.next_occurrence(ikey)

        cached = self._journal.lookup_record(ikey, occurrence)
        if cached is not None:
            await self._emit(
                WorkflowEvent(
                    kind="interrupt_replayed",
                    title=key,
                    call_key=ikey,
                    occurrence=occurrence,
                    result_text=cached.result_text,
                )
            )
            return json.loads(cached.result_text)

        throw_if_aborted(self._signal)

        if key in self._resume:
            result_text = _encode_json_value(
                f"wf.interrupt({key!r})", "answer", self._resume.pop(key)
            )
            self._journal.record(ikey, occurrence, result_text, record_kind="interrupt")
            await self._emit(
                WorkflowEvent(
                    kind="interrupt_resolved",
                    title=key,
                    call_key=ikey,
                    occurrence=occurrence,
                    result_text=result_text,
                )
            )
            await self._maybe_snapshot()
            return json.loads(result_text)

        # Validated before the event is emitted so a bad payload fails loudly
        # instead of breaking the run store's encoder mid-suspend.
        _encode_json_value(f"wf.interrupt({key!r})", "payload", payload)
        await self._emit(
            WorkflowEvent(
                kind="interrupt_requested",
                title=key,
                call_key=ikey,
                occurrence=occurrence,
                structured_output={"payload": payload},
            )
        )
        raise WorkflowSuspended(key, payload)

    def _resolve_timeout_ms(self, timeout_ms: float | None) -> float | None:
        """Per-call value wins; ``0`` or negative opts out of the context default."""
        if timeout_ms is not None:
            return timeout_ms if timeout_ms > 0 else None
        default = self._step_timeout_ms
        return default if default is not None and default > 0 else None

    async def _call_with_policy(
        self,
        run: Callable[[], Awaitable[WorkflowJournalRecord]],
        *,
        label: str,
        timeout_ms: float | None,
        retry: RetryOptions | None,
    ) -> WorkflowJournalRecord:
        """Run the call under its timeout and retry policy.

        The timeout is per *attempt*, so a retry gets the full budget again.
        With neither policy set there is no ``wait_for`` and no ``with_retry``
        frame at all, so an unset policy costs nothing.
        """
        resolved = self._resolve_timeout_ms(timeout_ms)
        if resolved is None and retry is None:
            return await run()

        async def attempt(_attempt: int) -> WorkflowJournalRecord:
            if resolved is None:
                return await run()
            try:
                return await asyncio.wait_for(run(), timeout=resolved / 1000.0)
            except asyncio.TimeoutError as exc:
                raise WorkflowTimeoutError(f"{label} timed out after {resolved:g}ms") from exc

        if retry is None:
            return await attempt(0)
        return await with_retry(
            attempt,
            signal=self._signal,
            options=retry,
            retry_on=_workflow_retry_on,
        )

    async def _journaled_call(
        self,
        *,
        key: str,
        kinds: tuple[str, str, str],
        title: str,
        label: str,
        run: Callable[[], Awaitable[WorkflowJournalRecord]],
        subagent_type: str = "",
        timeout_ms: float | None = None,
        retry: RetryOptions | None = None,
    ) -> WorkflowJournalRecord:
        """Replay one journaled call, or execute it and journal the result.

        Args:
            key: Content hash identifying the call (``call_key``/``step_key``).
            kinds: The ``(start, end, replayed)`` event kinds to emit.
            title: Display name carried on every emitted event.
            label: How the call is named in error messages.
            run: Performs the work and returns the record to journal. It is not
                called at all when the journal already holds this occurrence.
            subagent_type: Carried on the events for agent calls; empty for steps.
            timeout_ms: Wall-clock budget for one attempt; see
                :meth:`_resolve_timeout_ms`.
            retry: Backoff policy for failed attempts; ``None`` means one attempt.

        Returns:
            The journaled record, whether replayed or freshly produced.
        """
        start_kind, end_kind, replayed_kind = kinds
        occurrence = self._journal.next_occurrence(key)

        cached = self._journal.lookup_record(key, occurrence)
        if cached is not None:
            await self._emit(
                WorkflowEvent(
                    kind=cast(Any, replayed_kind),
                    title=title,
                    call_key=key,
                    occurrence=occurrence,
                    subagent_type=subagent_type,
                    result_text=cached.result_text,
                    structured_output=cached.structured_output,
                    structured_error=cached.structured_error,
                )
            )
            return cached

        throw_if_aborted(self._signal)
        await self._emit(
            WorkflowEvent(
                kind=cast(Any, start_kind),
                title=title,
                call_key=key,
                occurrence=occurrence,
                subagent_type=subagent_type,
            )
        )

        record = await self._call_with_policy(run, label=label, timeout_ms=timeout_ms, retry=retry)
        self._journal.record(
            key,
            occurrence,
            record.result_text,
            structured_output=record.structured_output,
            structured_error=record.structured_error,
            record_kind=record.record_kind,
        )
        await self._emit(
            WorkflowEvent(
                kind=cast(Any, end_kind),
                title=title,
                call_key=key,
                occurrence=occurrence,
                subagent_type=subagent_type,
                result_text=record.result_text,
                structured_output=record.structured_output,
                structured_error=record.structured_error,
            )
        )
        await self._maybe_snapshot()
        return record

    def _agent_backpressure(self) -> AbstractAsyncContextManager[Any]:
        """Cap how many ``wf.agent`` calls hold a live provider slot at once.

        Separate from ``max_concurrency``, which shapes a fan-out: this one
        limits real provider pressure regardless of how the branches nest.
        Deadlock-free because it gates a leaf — a subagent run never re-enters
        ``wf.agent``. Created on first use, so leaving it unset costs nothing.
        """
        if self._max_agent_concurrency <= 0:
            return nullcontext()
        if self._agent_gate is None:
            self._agent_gate = asyncio.Semaphore(self._max_agent_concurrency)
        return self._agent_gate

    def _parallel_semaphore(self) -> asyncio.Semaphore:
        """Pick the semaphore gating one ``parallel`` call's branches.

        A root-level fan-out shares this context's semaphore.  A *nested*
        ``parallel`` gets its own, because its caller is already holding a slot
        in the shared one — reusing it would deadlock as soon as the outer
        fan-out is as wide as ``max_concurrency``.
        """
        if self._branch_marker in _PARALLEL_BRANCH.get(frozenset()):
            return asyncio.Semaphore(self._max_concurrency)
        return self._semaphore

    async def parallel(self, thunks: Sequence[Callable[[], Awaitable[Any]]]) -> list[Any]:
        """Run *thunks* concurrently (capped by ``max_concurrency``).

        Results are returned in input order.  The semaphore gates the thunks
        themselves — one slot per branch, one budget per nesting level.  When a
        branch raises, the remaining branches are cancelled and drained before
        the error propagates, so a failed fan-out stops spending immediately.
        """
        tasks = self._spawn_branches(thunks)
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            await _cancel_and_drain(tasks)
            raise

    async def settled(self, thunks: Sequence[Callable[[], Awaitable[Any]]]) -> list[StepOutcome]:
        """Like :meth:`parallel`, but a failing branch does not cancel its siblings.

        Every slot holds either the branch's value or its exception, in input
        order — use it when partial results are worth having. Cancellation and
        :class:`WorkflowSuspended` are control flow, not outcomes: they
        propagate instead of landing in a slot.

        Args:
            thunks: Zero-argument callables returning awaitables, one per branch.

        Returns:
            One :class:`StepOutcome` per input thunk, in input order.
        """
        tasks = self._spawn_branches(thunks)
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            await _cancel_and_drain(tasks)
            raise

        outcomes: list[StepOutcome] = []
        for result in results:
            if isinstance(result, (asyncio.CancelledError, WorkflowSuspended)):
                raise result
            if isinstance(result, BaseException):
                outcomes.append(StepOutcome(ok=False, error=result))
            else:
                outcomes.append(StepOutcome(ok=True, value=result))
        return outcomes

    def _spawn_branches(
        self, thunks: Sequence[Callable[[], Awaitable[Any]]]
    ) -> list[asyncio.Future[Any]]:
        """Start one gated task per thunk, marking each as inside this context."""
        semaphore = self._parallel_semaphore()

        async def gated(thunk: Callable[[], Awaitable[Any]]) -> Any:
            token = _PARALLEL_BRANCH.set(_PARALLEL_BRANCH.get(frozenset()) | {self._branch_marker})
            try:
                async with semaphore:
                    return await thunk()
            finally:
                _PARALLEL_BRANCH.reset(token)

        return [asyncio.ensure_future(gated(thunk)) for thunk in thunks]

    async def pipeline(
        self,
        items: Sequence[Any],
        *stages: Callable[[Any], Awaitable[Any]],
    ) -> list[Any]:
        """Run each item through all stages independently — no barrier between
        stages, so item B's stage 1 and item A's stage 2 can overlap."""

        def make_chain(item: Any) -> Callable[[], Awaitable[Any]]:
            async def chain() -> Any:
                value = item
                for stage in stages:
                    value = await stage(value)
                return value

            return chain

        return await self.parallel([make_chain(item) for item in items])


def workflow_checkpoint(workflow_name: str, phase: str) -> Any:
    """A minimal ``RunCheckpoint`` marking where a workflow run stands.

    A workflow's resumable state lives in the event log (and, when enabled, the
    journal snapshot in ``extension_state``), so the checkpoint itself only has
    to carry the run's phase.

    Args:
        workflow_name: Recorded as the checkpoint's prompt, for readability.
        phase: A ``RunPhase`` — ``"workflow_running"``, ``"workflow_suspended"``
            or ``"completed"``.

    Returns:
        A fresh ``RunCheckpoint`` with an empty ``extension_state``.
    """
    from ..run_store import RunCheckpoint
    from ..types import Usage

    return RunCheckpoint(
        phase=cast(Any, phase),
        prompt=workflow_name,
        turn_index=0,
        total_usage=Usage(),
    )


async def _cancel_and_drain(tasks: Sequence[asyncio.Future[Any]]) -> None:
    """Cancel every task and wait for them to settle, so nothing is left running."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _encode_json_value(label: str, what: str, value: Any) -> str:
    """Encode a journaled value, or explain why it can never replay."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} {what} is not JSON-serializable: {exc}") from exc


def _merge_run_options(
    run_options: RunOptions | None,
    *,
    output_schema: Any = None,
    final_tool_name: str | None = None,
) -> RunOptions | None:
    if output_schema is None and final_tool_name is None:
        return run_options
    opts = run_options or RunOptions()
    updates: dict[str, Any] = {}
    if output_schema is not None:
        updates["output_schema"] = output_schema
    if final_tool_name is not None:
        updates["final_tool_name"] = final_tool_name
    return dataclasses.replace(opts, **updates)


def _run_options_fingerprint(run_options: RunOptions | None) -> str:
    if run_options is None:
        return ""
    payload: dict[str, Any] = {}
    for field in dataclasses.fields(run_options):
        if field.name in {"signal", "budget"}:
            continue
        value = getattr(run_options, field.name)
        if value is not None:
            payload[field.name] = _fingerprint_value(value)
    if not payload:
        return ""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _call_options_fingerprint(
    *,
    tools: list[str] | None,
    run_options: RunOptions | None,
) -> str:
    run_options_fingerprint = _run_options_fingerprint(run_options)
    if tools is None:
        return run_options_fingerprint
    payload: dict[str, Any] = {"tools": tools}
    if run_options_fingerprint:
        payload["run_options"] = json.loads(run_options_fingerprint)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _fingerprint_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _fingerprint_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {str(k): _fingerprint_value(v) for k, v in sorted(value.items(), key=str)}
    if isinstance(value, (list, tuple)):
        return [_fingerprint_value(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f"{value.__class__.__module__}.{value.__class__.__qualname__}:{value!r}"
