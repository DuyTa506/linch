"""Run-scoped persistence, top-of-turn drains, and lifecycle helpers.

The mutable run state itself remains assembled by ``runner.py`` for now; this
module owns the low-level operations that are shared by each loop phase and
must retain their persistence/yield ordering.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from functools import partial
from typing import Any, cast

from .._telemetry import end_step, start_step, store_call
from .._wire import UnsupportedWireVersionError
from ..errors import AbortError, ConfigError
from ..events import (
    BackgroundWorkerEvent,
    BudgetEvent,
    ErrorEvent,
    Event,
    ModelCallEvent,
    ResultEvent,
    UsageEvent,
    UserEvent,
)
from ..hooks import (
    HookDispatcher,
    HookEvent,
    ProviderCallStartContext,
    ProviderCallStopContext,
    TurnStartContext,
    TurnStopContext,
)
from ..hooks.contexts import _AgentResult
from ..model_calls import (
    ModelCallIntent,
    _ModelCallError,
    _ModelCallLedger,
    _primary_active_binding,
)
from ..run_store import (
    LeaseLostError,
    ModelInputSnapshotError,
    RunCheckpoint,
    RunRecord,
    decode_model_input_snapshot,
)
from ..session import Session
from ..types import Message, ProviderRequest, TextBlock, ToolResultBlock, ToolUseBlock, Usage
from .checkpoint import (
    RunEventBuffer,
    _alignment_queue_to_dicts,
    _background_workers_to_dict,
    _consumed_auxiliary_receipt_ids_from_events,
    _flush_events,
    _loop_guard_is_active,
    _loop_guard_state_from_dict,
    _loop_guard_state_to_dict,
    _model_call_runtime_from_extension,
    _model_call_runtime_to_extension,
    _persist_event,
    _skill_overlay_to_dict,
)
from .dispatch import (
    dispatch_after_provider_call,
    dispatch_before_final_answer,
    dispatch_before_provider_call,
    dispatch_lifecycle,
    dispatch_stop,
    dispatch_user_prompt,
)
from .request import build_user_message
from .terminals import _budget_exhausted_tail


def _error_payload(exc: Exception) -> dict[str, object]:
    """Build the stable error envelope used by terminal ErrorEvents."""
    # The journal has one known persistence wrapper; report its backend cause.
    if (
        isinstance(exc, _ModelCallError)
        and exc.code == "persistence_failed"
        and isinstance(exc.__cause__, Exception)
    ):
        exc = exc.__cause__
    retryable = getattr(exc, "retryable", False)
    payload: dict[str, object] = {
        "name": exc.__class__.__name__,
        "message": str(exc),
        "retryable": retryable,
    }
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        payload["status"] = status
    return payload


def require_model_input_snapshot_store(agent: Any) -> Any:
    """Validate and return the store capabilities required by exact replay."""
    store = getattr(agent, "run_store", None)
    if store is None:
        raise ConfigError("exact_model_input requires a run_store")
    missing = [
        name
        for name in ("save", "load", "delete", "prune")
        if not callable(getattr(store, name, None))
    ]
    if missing:
        raise ConfigError(
            "exact_model_input requires run_store model-input snapshot capabilities: "
            + ", ".join(missing)
        )
    return store


def checkpointable_hooks(hooks: list[Any]) -> dict[str, Any]:
    """Return checkpoint-capable hooks keyed by their stable namespace."""
    out: dict[str, Any] = {}
    for hook in hooks:
        snapshot = getattr(hook, "checkpoint_state", None)
        restore = getattr(hook, "restore_checkpoint_state", None)
        if snapshot is None and restore is None:
            continue
        name = str(getattr(hook, "name", hook.__class__.__name__))
        if not callable(snapshot) or not callable(restore):
            raise ConfigError(
                f"checkpointable hook {name!r} must implement both "
                "checkpoint_state(session, run_id) and "
                "restore_checkpoint_state(state, session, run_id)"
            )
        key = getattr(hook, "checkpoint_key", None)
        if not isinstance(key, str) or not key:
            raise ConfigError(
                f"checkpointable hook {name!r} must define a non-empty string checkpoint_key"
            )
        if key in out:
            raise ConfigError(f"duplicate checkpoint_key {key!r} on agent hooks")
        out[key] = hook
    return out


def checkpoint_extension_state(
    checkpointable_hooks: dict[str, Any], session: Session, run_id: str
) -> dict[str, dict[str, Any]]:
    """Capture JSON-safe state from checkpointable hooks."""
    state_by_key: dict[str, dict[str, Any]] = {}
    for key, hook in checkpointable_hooks.items():
        try:
            state = hook.checkpoint_state(session, run_id)
        except LeaseLostError:
            raise
        except Exception as exc:
            raise RuntimeError(f"checkpointable hook {key!r} failed to save state") from exc
        if not isinstance(state, dict) or not all(isinstance(name, str) for name in state):
            raise ConfigError(f"checkpointable hook {key!r} must return a dict with string keys")
        if not state:
            continue
        try:
            serialized = json.dumps(state, allow_nan=False)
            restored = json.loads(serialized)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"checkpointable hook {key!r} returned non-JSON-safe checkpoint state"
            ) from exc
        if not isinstance(restored, dict):
            raise RuntimeError(f"checkpointable hook {key!r} produced invalid checkpoint state")
        state_by_key[key] = restored
    return state_by_key


def restore_checkpointable_hooks(
    checkpointable_hooks: dict[str, Any],
    extension_state: dict[str, Any],
    session: Session,
    run_id: str,
) -> None:
    """Restore active hook namespaces from a checkpoint."""
    for key, hook in checkpointable_hooks.items():
        if key not in extension_state:
            continue
        state = extension_state[key]
        if not isinstance(state, dict) or not all(isinstance(name, str) for name in state):
            raise ConfigError(f"checkpoint state for hook {key!r} must be a dict with string keys")
        try:
            hook.restore_checkpoint_state(dict(state), session, run_id)
        except LeaseLostError:
            raise
        except Exception as exc:
            raise RuntimeError(f"checkpointable hook {key!r} failed to restore state") from exc


async def drain_pending_notifications(session: Session, run_id: str) -> AsyncIterator[Event]:
    """Inject pending background-worker notifications into provider history."""
    durability = getattr(session.agent, "durability", None)
    if getattr(durability, "durable_inbox", False):
        notifications = getattr(session, "pending_notifications", None)
        if notifications:
            raise ConfigError(
                "durable_inbox cannot consume pending_notifications; use await Session.notify(...)"
            )
        commit = getattr(session.store, "commit_inbox", None)
        enqueue = getattr(session.store, "enqueue_inbox", None)
        if not callable(commit) or not callable(enqueue):
            raise ConfigError(
                "durable_inbox requires a SessionStore implementing "
                "enqueue_inbox() and commit_inbox()"
            )
        stored = await store_call(
            session,
            "session.commit_inbox",
            cast(Any, commit)(session.id, after_seq=session._last_seq),
        )
        if not stored:
            return
        session._track_seqs(stored)
        for row in stored:
            note = row.message
            session.session_log.append(note)
            event: Event = UserEvent(message=note, subtype="notification")
            await _persist_event(session, run_id, event)
            yield event
        return
    notifications = getattr(session, "pending_notifications", None)
    if not notifications:
        return
    to_drain = list(notifications)
    notifications.clear()
    for note in to_drain:
        await store_call(session, "session.append", session.append([note]))
        event: Event = UserEvent(message=note, subtype="notification")
        await _persist_event(session, run_id, event)
        yield event


def render_peer_message(message: Any) -> Message:
    """Wrap a drained mailbox message as a provider-visible user message."""
    from xml.sax.saxutils import escape

    parts = [
        "<peer-message>",
        f"<from>{escape(message.sender)}</from>",
        f"<type>{escape(message.type)}</type>",
    ]
    if message.request_id:
        parts.append(f"<request-id>{escape(message.request_id)}</request-id>")
    if message.in_reply_to:
        parts.append(f"<in-reply-to>{escape(message.in_reply_to)}</in-reply-to>")
    parts.append(f"<content>{escape(message.content)}</content>")
    parts.append("</peer-message>")
    return Message(role="user", content=[TextBlock(text="".join(parts))])


async def drain_mailbox(session: Session, run_id: str) -> AsyncIterator[Event]:
    """Drain peer messages for this session into provider-visible history."""
    mailbox = getattr(session.agent, "mailbox", None)
    address = getattr(session, "mailbox_address", None)
    if mailbox is None or not address:
        return
    durability = getattr(session.agent, "durability", None)
    if getattr(durability, "durable_inbox", False):
        claim = getattr(mailbox, "claim", None)
        ack = getattr(mailbox, "ack", None)
        release = getattr(mailbox, "release", None)
        if not all(callable(method) for method in (claim, ack, release)):
            raise ConfigError(
                "durable_inbox with mailbox requires ClaimableMailbox claim(), ack(), and release()"
            )
        claims = await cast(Any, claim)(
            address,
            owner=f"session:{session.id}",
            lease_s=cast(Any, durability).delivery_lease_s,
        )
        delivered = []
        try:
            for claimed in claims:
                message = claimed.message
                note = render_peer_message(message)
                await store_call(
                    session,
                    "session.enqueue_inbox",
                    session.notify(
                        note,
                        delivery_id=f"mailbox:{message.id}",
                        source="mailbox",
                        metadata={"message_id": message.id, "recipient": message.recipient},
                    ),
                )
                await cast(Any, ack)([claimed])
                delivered.append(claimed)
        except BaseException:
            pending = [item for item in claims if item not in delivered]
            if pending:
                await cast(Any, release)(pending)
            raise
        async for event in drain_pending_notifications(session, run_id):
            yield event
        return
    messages = await mailbox.drain(address)
    for message in messages:
        note = render_peer_message(message)
        await store_call(session, "session.append", session.append([note]))
        event: Event = UserEvent(message=note, subtype="notification")
        await _persist_event(session, run_id, event)
        yield event


async def drain_alignment(
    session: Session,
    run_id: str,
    dispatch_user_prompt_fn: Callable[
        [str, list[dict[str, str]] | None, str],
        Awaitable[tuple[str, list[dict[str, str]] | None, list[Event], str | None]],
    ],
) -> AsyncIterator[Event]:
    """Apply queued steering prompts at the top-of-turn boundary."""
    queue = getattr(session, "alignment_queue", None)
    if not queue:
        return
    entries = list(queue)
    queue.clear()
    for entry in entries:
        try:
            try:
                prompt, images, hook_events, block_reason = await dispatch_user_prompt_fn(
                    entry.prompt, entry.images, "align"
                )
            except LeaseLostError:
                raise
            except Exception as exc:
                if not entry.future.done():
                    entry.future.set_exception(exc)
                error_event: Event = ErrorEvent(error=_error_payload(exc))
                await _persist_event(session, run_id, error_event)
                yield error_event
                continue
            for hook_event in hook_events:
                await _persist_event(session, run_id, hook_event)
                yield hook_event
            if block_reason is not None:
                if not entry.future.done():
                    entry.future.set_exception(ConfigError(block_reason))
                continue
            user_message = build_user_message(prompt, images)
            await store_call(session, "session.append", session.append([user_message]))
            event: Event = UserEvent(message=user_message, subtype="alignment")
            await _persist_event(session, run_id, event)
            if not entry.future.done():
                entry.future.set_result(None)
            yield event
        except BaseException as exc:
            if not entry.future.done():
                entry.future.set_exception(exc)
            raise


async def drain_child_events(session: Session, run_id: str) -> AsyncIterator[Event]:
    """Surface accumulated subagent events at the top-of-turn boundary."""
    pending = getattr(session, "pending_child_events", None)
    if not pending:
        return
    to_drain = list(pending)
    pending.clear()
    for event in to_drain:
        if not isinstance(event, BackgroundWorkerEvent):
            await _persist_event(session, run_id, event)
        yield event


async def cancel_background_workers(session: Session) -> None:
    """Cancel and await this run's worker/background tasks."""
    import asyncio

    tasks: list[asyncio.Task[Any]] = []
    workers = getattr(session, "workers", None)
    for handle in (workers or {}).values():
        task = getattr(handle, "task", None)
        if isinstance(task, asyncio.Task):
            tasks.append(task)
    for task in getattr(session, "background_tasks", None) or []:
        if isinstance(task, asyncio.Task):
            tasks.append(task)
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def maybe_save_provider_snapshot(session: Session) -> None:
    """Persist the compacted provider view when the store supports it."""
    saver = getattr(session.store, "save_provider_snapshot", None)
    if saver is None or not getattr(session, "_seq_cacheable", True):
        return
    from ..sessions.store import ProviderViewSnapshot

    snapshot = ProviderViewSnapshot(
        provider_view=list(session.provider_view),
        covers_seq=getattr(session, "_last_seq", session.session_log.history_count),
        projection_id=(session.last_compaction_info or {}).get("projection_id"),
    )
    try:
        await store_call(session, "session.save_provider_snapshot", saver(session.id, snapshot))
    except LeaseLostError:
        raise
    except Exception as exc:
        logging.getLogger(__name__).warning("Failed to save provider snapshot: %s", exc)


class TurnLifecycle:
    """Track the open turn/provider-call lifecycle callbacks for one run."""

    def __init__(
        self,
        session: Session,
        run_id: str,
        dispatch: Callable[[HookEvent, Any], Awaitable[None]],
    ) -> None:
        self._session = session
        self._run_id = run_id
        self._dispatch = dispatch
        self.active_turn_index: int | None = None
        self.active_provider_call: tuple[int, str, float] | None = None

    async def start_turn(self, turn_index: int) -> None:
        start_step(self._session, turn_index)
        await self._dispatch(
            HookEvent.TURN_START,
            TurnStartContext(
                session=self._session,
                run_id=self._run_id,
                turn_index=turn_index,
                deps=getattr(self._session, "run_deps", None),
            ),
        )
        self.active_turn_index = turn_index

    async def end_active_turn(self) -> None:
        if self.active_turn_index is None:
            return
        turn_index = self.active_turn_index
        self.active_turn_index = None
        end_step(self._session)
        await self._dispatch(
            HookEvent.TURN_STOP,
            TurnStopContext(
                session=self._session,
                run_id=self._run_id,
                turn_index=turn_index,
                deps=getattr(self._session, "run_deps", None),
            ),
        )

    async def start_provider_call(self, turn_index: int, model: str) -> None:
        started_at = time.perf_counter()
        await self._dispatch(
            HookEvent.PROVIDER_CALL_START,
            ProviderCallStartContext(
                session=self._session,
                run_id=self._run_id,
                turn_index=turn_index,
                deps=getattr(self._session, "run_deps", None),
                model=model,
            ),
        )
        self.active_provider_call = (turn_index, model, started_at)

    async def end_active_provider_call(
        self, *, stop_reason: str, usage: Usage | None = None
    ) -> None:
        if self.active_provider_call is None:
            return
        turn_index, model, started_at = self.active_provider_call
        self.active_provider_call = None
        await self._dispatch(
            HookEvent.PROVIDER_CALL_STOP,
            ProviderCallStopContext(
                session=self._session,
                run_id=self._run_id,
                turn_index=turn_index,
                deps=getattr(self._session, "run_deps", None),
                model=model,
                stop_reason=stop_reason,
                usage=usage or Usage(),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            ),
        )

    async def close_active_spans(self, *, stop_reason: str = "error") -> None:
        await self.end_active_provider_call(stop_reason=stop_reason)
        await self.end_active_turn()


class RunRuntime:
    """Per-run state and persistence operations shared by loop phases.

    ``runner.py`` still owns turn routing and terminal cleanup.  This object is
    the single owner of the run journal, checkpoint writer, ledger totals, and
    exact-input snapshot lifecycle so phase contexts can use bound methods
    without copying those mutable cells.
    """

    def __init__(
        self,
        *,
        session: Session,
        prompt: str,
        opts: Any,
        run_id: str,
        run_record: RunRecord | None,
        resume_checkpoint: RunCheckpoint | None,
    ) -> None:
        self.session = session
        self.agent = session.agent
        self.prompt = prompt
        self.opts = opts
        self.run_id = run_id
        self.run_record = run_record
        self.resume_checkpoint = resume_checkpoint
        self.started = 0.0
        self.exact_model_input = False
        self.model_input_store: Any = None
        self.stored_events: list[Any] = []
        self.checkpoint: RunCheckpoint
        self.signal: Any = None
        self.ledger: _ModelCallLedger
        self.total = Usage()
        self.running_cost: float | None = None
        self.active_primary: dict[str, Any] | None = None
        self.budget: Any = None
        self.effective_final_tool: str | None = None
        self.guard: Any = None
        self.guard_state: Any = None
        self.truncation_recovery: Any = None
        self.truncation_attempts = 0
        self.truncation_prefix = ""
        self.pending_truncation_feedback: str | None = None
        self.force_final_pending = False
        self.terminal_result: _AgentResult | None = None
        self.align_defer_turn: int | None = None
        self.forced_compactions_used = [0]
        self.checkpointable_hooks: dict[str, Any] = {}
        self.hook_dispatcher: HookDispatcher
        self.dispatch_user_prompt: Callable[..., Awaitable[Any]]
        self.dispatch_lifecycle: Callable[..., Awaitable[Any]]
        self.dispatch_before_provider_call: Callable[..., Awaitable[Any]]
        self.dispatch_after_provider_call: Callable[..., Awaitable[Any]]
        self.dispatch_before_final_answer: Callable[..., Awaitable[Any]]
        self.dispatch_stop: Callable[..., Awaitable[Any]]
        self._checkpointable_hooks_fn: Callable[..., dict[str, Any]] | None = None
        self._checkpoint_extension_state_fn: Callable[..., dict[str, Any]] | None = None
        self.lease_lost: LeaseLostError | None = None

    async def _attempt(
        self, operation: str, awaitable: Callable[[], Awaitable[Any]], original: Exception
    ) -> Exception | None:
        """Attempt terminal persistence once, keeping the causal failure visible."""
        if self.lease_lost is not None:
            return self.lease_lost
        try:
            await awaitable()
        except LeaseLostError as exc:
            self.lease_lost = exc
            await cancel_background_workers(self.session)
            return exc
        except Exception as secondary:
            if secondary.__cause__ is None and secondary.__context__ is None:
                secondary.__context__ = original
            logging.getLogger(__name__).warning(
                "run %s terminal %s failed after %s (%s): %s",
                self.run_id,
                operation,
                type(original).__name__,
                original,
                secondary,
                exc_info=(type(secondary), secondary, secondary.__traceback__),
            )
            return secondary
        return None

    @classmethod
    async def create(
        cls,
        session: Session,
        prompt: str,
        opts: Any,
        *,
        run_id: str,
        run_record: RunRecord | None,
        resume_checkpoint: RunCheckpoint | None,
        exact_model_input_enabled: Callable[[Any], bool],
        require_model_input_snapshot_store: Callable[[Any], Any],
        checkpointable_hooks: Callable[[list[Any]], dict[str, Any]],
        restore_checkpointable_hooks: Callable[..., None],
        checkpoint_extension_state: Callable[..., dict[str, Any]],
        effective_final_tool_name: Callable[..., str | None],
        loop_guard_state_factory: Callable[[], Any],
        skill_overlay_from_dict: Callable[[Any], Any],
        alignment_entries_from_checkpoint: Callable[[Any], list[Any]],
        model_call_runtime_from_extension: Callable[..., Any] = _model_call_runtime_from_extension,
    ) -> RunRuntime:
        runtime = cls(
            session=session,
            prompt=prompt,
            opts=opts,
            run_id=run_id,
            run_record=run_record,
            resume_checkpoint=resume_checkpoint,
        )
        agent = runtime.agent
        runtime.exact_model_input = exact_model_input_enabled(agent)
        runtime.model_input_store = (
            require_model_input_snapshot_store(agent) if runtime.exact_model_input else None
        )
        session.active_run_id = run_id
        initial_event_seq = 0
        if resume_checkpoint is not None and agent.run_store is not None:
            runtime.stored_events = await store_call(
                session, "run.load_events", agent.run_store.load_events(run_id)
            )
            initial_event_seq = max((row.seq for row in runtime.stored_events), default=0)
        session._event_journal = RunEventBuffer(
            agent.run_store,
            run_id,
            initial_seq=initial_event_seq,
            session=session,
        )
        session.active_model = None
        session.fallback_index = 0
        if resume_checkpoint is not None:
            core_state = resume_checkpoint.extension_state.get("linch.core", {})
            if isinstance(core_state, dict):
                active_model = core_state.get("active_model")
                fallback_index = core_state.get("fallback_index")
                session.active_model = active_model if isinstance(active_model, str) else None
                session.fallback_index = (
                    max(0, fallback_index) if isinstance(fallback_index, int) else 0
                )
        runtime.started = time.time()
        runtime.total = Usage()
        runtime.running_cost = None

        hooks = list(getattr(agent, "hooks", None) or [])
        runtime.checkpointable_hooks = checkpointable_hooks(hooks)
        runtime.hook_dispatcher = HookDispatcher(hooks)
        runtime.dispatch_user_prompt = partial(
            dispatch_user_prompt,
            runtime.hook_dispatcher,
            session,
            run_id,
        )
        runtime.dispatch_lifecycle = partial(dispatch_lifecycle, runtime.hook_dispatcher)
        runtime.dispatch_before_provider_call = partial(
            dispatch_before_provider_call,
            runtime.hook_dispatcher,
            session,
            run_id,
        )
        runtime.dispatch_after_provider_call = partial(
            dispatch_after_provider_call,
            runtime.hook_dispatcher,
            session,
            run_id,
        )
        runtime.dispatch_before_final_answer = partial(
            dispatch_before_final_answer,
            runtime.hook_dispatcher,
            session,
            run_id,
        )
        runtime.dispatch_stop = partial(dispatch_stop, runtime.hook_dispatcher, session, run_id)
        session.run_deps = opts.deps if opts.deps is not None else getattr(agent, "deps", None)
        if resume_checkpoint is not None:
            restore_checkpointable_hooks(
                runtime.checkpointable_hooks,
                resume_checkpoint.extension_state,
                session,
                run_id,
            )
        runtime.budget = (
            opts.budget
            or getattr(session, "inherited_budget", None)
            or getattr(agent, "budget", None)
        )
        session.active_budget = runtime.budget
        runtime.effective_final_tool = effective_final_tool_name(agent, opts)
        runtime.guard = getattr(agent, "loop_guard", None)
        runtime.guard_state = (
            loop_guard_state_factory() if _loop_guard_is_active(runtime.guard) else None
        )
        runtime.truncation_recovery = getattr(agent, "truncation_recovery", None)
        runtime.checkpoint = resume_checkpoint or RunCheckpoint(
            phase="started",
            prompt=prompt,
            turn_index=0,
            total_usage=runtime.total,
        )
        if resume_checkpoint is not None:
            if _loop_guard_is_active(runtime.guard):
                restored = _loop_guard_state_from_dict(resume_checkpoint.loop_guard_state)
                if restored is not None:
                    runtime.guard_state = restored
            runtime.force_final_pending = resume_checkpoint.force_final_pending
            session.pending_skill_overlay = skill_overlay_from_dict(
                resume_checkpoint.pending_skill_overlay
            )
            session.current_turn_allowed_tools = resume_checkpoint.current_turn_allowed_tools
            session.current_turn_permission_decisions = dict(resume_checkpoint.permission_decisions)
            runtime.truncation_attempts = resume_checkpoint.truncation_attempts
            runtime.truncation_prefix = resume_checkpoint.truncation_prefix
            runtime.pending_truncation_feedback = resume_checkpoint.pending_truncation_feedback
            if resume_checkpoint.pending_alignment:
                session.alignment_queue.extend(
                    alignment_entries_from_checkpoint(resume_checkpoint.pending_alignment)
                )
            if resume_checkpoint.phase in {
                "provider_pending",
                "assistant_appended",
                "permission_pending",
                "tool_batch_pending",
                "tool_executing",
                "tool_results_appended",
            }:
                runtime.align_defer_turn = resume_checkpoint.turn_index

        runtime._checkpointable_hooks_fn = checkpointable_hooks
        runtime._checkpoint_extension_state_fn = checkpoint_extension_state
        runtime.active_primary, journal_count = model_call_runtime_from_extension(
            runtime.checkpoint.extension_state
        )
        model_call_events = [
            row.event
            for row in runtime.stored_events
            if isinstance(getattr(row, "event", None), ModelCallEvent)
        ]
        consumed_auxiliary_receipt_ids = _consumed_auxiliary_receipt_ids_from_events(
            runtime.stored_events
        )
        runtime.ledger = _ModelCallLedger(
            run_id,
            budget=runtime.budget,
            persist=runtime.persist_model_call if agent.run_store is not None else None,
        )
        session._model_call_ledger = runtime.ledger
        if model_call_events or journal_count or consumed_auxiliary_receipt_ids:
            try:
                await runtime.ledger.restore(
                    None,
                    model_call_events,
                    consumed_auxiliary_receipt_ids=consumed_auxiliary_receipt_ids,
                )
            except _ModelCallError as exc:
                if consumed_auxiliary_receipt_ids:
                    raise ConfigError("invalid durable compaction usage projection") from exc
                raise
            if resume_checkpoint is not None:
                expected = resume_checkpoint.total_usage
                actual = runtime.ledger.total_usage
                if any(
                    getattr(actual, field) < getattr(expected, field)
                    for field in (
                        "input_tokens",
                        "output_tokens",
                        "cache_read_tokens",
                        "cache_creation_tokens",
                    )
                ):
                    raise ConfigError(
                        "Cannot resume checkpoint with nonzero aggregate usage without "
                        "receipt provenance"
                    )
            runtime.total = runtime.ledger.total_usage
            runtime.running_cost = runtime.ledger.total_cost_usd
        elif resume_checkpoint is not None and resume_checkpoint.total_usage != Usage():
            raise ConfigError(
                "Cannot resume checkpoint with nonzero aggregate usage without receipt provenance"
            )
        if journal_count is not None and len(model_call_events) < journal_count:
            raise ConfigError("model-call journal is missing records referenced by checkpoint")
        if runtime.active_primary is not None and resume_checkpoint is not None:
            delivery_id = runtime.active_primary["delivery_attempt_id"]
            if (
                resume_checkpoint.phase == "assistant_appended"
                and runtime.ledger.intent_for(delivery_id) is None
            ):
                raise ConfigError("active primary journal is missing its intent")
        return runtime

    async def persist_model_call(self, event: ModelCallEvent) -> None:
        await _persist_event(self.session, self.run_id, event)
        await _flush_events(self.session)

    def sync_ledger_totals(self) -> None:
        self.total = self.ledger.total_usage
        self.running_cost = self.ledger.total_cost_usd

    def sync_checkpoint_accounting(self) -> None:
        self.sync_ledger_totals()
        if not isinstance(self.checkpoint.extension_state, dict):
            raise ConfigError("RunCheckpoint.extension_state must be a dict")
        _model_call_runtime_to_extension(
            self.checkpoint.extension_state,
            ledger_snapshot=None,
            active_primary=self.active_primary,
            journal_count=self.ledger._journal_count(),
        )
        self.checkpoint.total_usage = self.total

    def build_run_result(
        self,
        subtype: str,
        stop_reason: str,
        *,
        duration_ms: int,
        error: dict[str, Any] | None = None,
    ) -> _AgentResult:
        """Build a terminal result from the ledger-owned run totals."""
        self.sync_ledger_totals()
        return _AgentResult(
            run_id=self.run_id,
            session_id=self.session.id,
            subtype=subtype,
            stop_reason=stop_reason,
            total_usage=self.total,
            duration_ms=duration_ms,
            error=error,
        )

    async def drain_auxiliary_usage(self) -> AsyncIterator[Event]:
        """Project settled auxiliary receipts before the next primary call."""
        for receipt in self.ledger.consume_auxiliary_receipts():
            self.sync_ledger_totals()
            if receipt.usage is None:
                continue
            event: Event = UsageEvent(
                usage=receipt.usage,
                cumulative=self.total,
                cost_usd=receipt.cost_usd,
                cumulative_cost_usd=self.running_cost,
                source="compaction",
                call_id=receipt.delivery_attempt_id,
                logical_call_id=receipt.logical_call_id,
                model=receipt.actual_model,
            )
            await _persist_event(self.session, self.run_id, event)
            yield event
            if self.budget is not None and self.budget.take_warning():
                warning: Event = BudgetEvent(
                    kind="warning",
                    spent_tokens=self.budget.spent_tokens,
                    spent_usd=self.budget.spent_usd,
                    max_tokens=self.budget.max_tokens,
                    max_cost_usd=self.budget.max_cost_usd,
                )
                await _persist_event(self.session, self.run_id, warning)
                yield warning

    async def emit_primary_usage(self, receipt: Any) -> AsyncIterator[Event]:
        """Publish the compatibility-shaped primary usage line."""
        self.sync_ledger_totals()
        if receipt.usage is None:
            return
        self.session.last_usage = receipt.usage
        event: Event = UsageEvent(
            usage=receipt.usage,
            cumulative=self.total,
            cost_usd=receipt.cost_usd,
            cumulative_cost_usd=self.running_cost,
        )
        await _persist_event(self.session, self.run_id, event)
        yield event
        if self.budget is not None and self.budget.take_warning():
            warning: Event = BudgetEvent(
                kind="warning",
                spent_tokens=self.budget.spent_tokens,
                spent_usd=self.budget.spent_usd,
                max_tokens=self.budget.max_tokens,
                max_cost_usd=self.budget.max_cost_usd,
            )
            await _persist_event(self.session, self.run_id, warning)
            yield warning

    async def emit_budget_exhausted(self) -> AsyncIterator[Event]:
        """Emit the legacy budget tail with ledger-owned totals."""
        self.sync_checkpoint_accounting()
        if self.budget is None:
            raise RuntimeError("budget terminal requested without a budget")
        duration_ms = int((time.time() - self.started) * 1000)
        self.terminal_result = self.build_run_result("error", "error", duration_ms=duration_ms)
        async for event in _budget_exhausted_tail(
            self.session,
            runtime=self,
            run_id=self.run_id,
            budget=self.budget,
            total=self.total,
            duration_ms=duration_ms,
            running_cost=self.running_cost,
            usage_complete=self.ledger.usage_complete,
        ):
            yield event

    async def emit_abort_terminal(
        self, *, close_active_spans: Callable[..., Awaitable[None]]
    ) -> AsyncIterator[Event]:
        """Persist and project the abort terminal while preserving event order."""
        await cancel_background_workers(self.session)
        duration_ms = int((time.time() - self.started) * 1000)
        original = AbortError("run aborted")
        self.terminal_result = self.build_run_result("aborted", "error", duration_ms=duration_ms)
        self.sync_checkpoint_accounting()
        await self._attempt(
            "lifecycle cleanup", lambda: close_active_spans(stop_reason="error"), original
        )
        event = ResultEvent(
            subtype="aborted",
            stop_reason="error",
            total_usage=self.total,
            duration_ms=duration_ms,
            total_cost_usd=self.running_cost,
            usage_complete=self.ledger.usage_complete,
        )
        failure = await self._attempt(
            "event append",
            lambda event=event: _persist_event(self.session, self.run_id, event),
            original,
        )
        if failure is None:
            failure = await self._attempt(
                "event flush", lambda: _flush_events(self.session), original
            )
            self.session._event_journal = None
        store = self.agent.run_store
        if failure is None and self.run_record is not None and store is not None:
            self.checkpoint.phase = "aborted"
            self.checkpoint.total_usage = self.total
            failure = await self._attempt(
                "run.save_checkpoint",
                lambda: store_call(
                    self.session,
                    "run.save_checkpoint",
                    store.save_checkpoint(self.run_id, self.checkpoint, status="aborted"),
                ),
                original,
            )
        if failure is not None:
            async for item in self.emit_exception_terminal(failure):
                yield item
        else:
            yield event

    async def emit_exception_terminal(
        self,
        exc: Exception,
        *,
        close_active_spans: Callable[..., Awaitable[None]] | None = None,
    ) -> AsyncIterator[Event]:
        """Persist the typed error pair before making failed status visible."""
        await cancel_background_workers(self.session)
        original = self.lease_lost or exc
        error = _error_payload(original)
        duration_ms = int((time.time() - self.started) * 1000)
        self.terminal_result = self.build_run_result(
            "error", "error", duration_ms=duration_ms, error=error
        )
        self.sync_checkpoint_accounting()
        if close_active_spans is not None:
            await self._attempt(
                "lifecycle cleanup", lambda: close_active_spans(stop_reason="error"), original
            )
        error_event = ErrorEvent(error=error)
        result_event = ResultEvent(
            subtype="error",
            stop_reason="error",
            total_usage=self.total,
            duration_ms=duration_ms,
            total_cost_usd=self.running_cost,
            usage_complete=self.ledger.usage_complete,
        )
        for event in (error_event, result_event):
            await self._attempt(
                "event append",
                lambda event=event: _persist_event(self.session, self.run_id, event),
                original,
            )
        await self._attempt("event flush", lambda: _flush_events(self.session), original)
        self.session._event_journal = None
        store = self.agent.run_store
        if self.run_record is not None and store is not None:
            await self._attempt(
                "run.mark_failed",
                lambda: store_call(
                    self.session,
                    "run.mark_failed",
                    store.mark_failed(self.run_id, self.checkpoint, error=error),
                ),
                original,
            )
        if self.lease_lost is not None:
            error = _error_payload(self.lease_lost)
            error["retryable"] = False
            error_event = ErrorEvent(error=error)
            result_event.stop_reason = "lease_lost"
            self.terminal_result = replace(
                self.terminal_result, stop_reason="lease_lost", error=error
            )
        yield error_event
        yield result_event

    async def best_effort_snapshot_cleanup(
        self,
        *,
        snapshot_id: str | None = None,
        keep_ids: tuple[str, ...] = (),
    ) -> None:
        if self.lease_lost is not None or self.model_input_store is None:
            return
        if snapshot_id is not None:
            try:
                await store_call(
                    self.session, "run.delete", self.model_input_store.delete(snapshot_id)
                )
            except LeaseLostError:
                raise
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Failed to delete model-input snapshot %s: %s", snapshot_id, exc
                )
        try:
            await store_call(
                self.session,
                "run.prune",
                self.model_input_store.prune(self.run_id, keep_ids=keep_ids),
            )
        except LeaseLostError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Failed to prune model-input snapshots for run %s: %s", self.run_id, exc
            )

    async def save_checkpoint(
        self,
        phase: str,
        *,
        status: str = "running",
        turn_index: int | None = None,
        assistant_message: Message | None | object = None,
        assistant_stop_reason: str | None | object = None,
        pending_tool_blocks: list[ToolUseBlock] | None | object = None,
        completed_tool_results: dict[str, ToolResultBlock] | None | object = None,
    ) -> None:
        if phase not in {"provider_pending", "assistant_appended"}:
            self.active_primary = None
        self.sync_checkpoint_accounting()
        store = self.agent.run_store
        if self.run_record is None or store is None:
            return
        buf = self.session._event_journal
        has_pending_events = buf is not None and bool(buf._pending)
        if not has_pending_events:
            await _flush_events(self.session)
        completed_snapshot_id = (
            self.checkpoint.model_input_snapshot_id
            if self.exact_model_input
            and phase != "provider_pending"
            and self.checkpoint.model_input_snapshot_id is not None
            else None
        )
        if completed_snapshot_id is not None:
            self.checkpoint.model_input_snapshot_id = None
        self.checkpoint.phase = phase  # type: ignore[assignment]
        if turn_index is not None:
            self.checkpoint.turn_index = turn_index
        self.checkpoint.total_usage = self.total
        if assistant_message is not None:
            self.checkpoint.assistant_message = (
                assistant_message if isinstance(assistant_message, Message) else None
            )
        if assistant_stop_reason is not None:
            self.checkpoint.assistant_stop_reason = (
                assistant_stop_reason if isinstance(assistant_stop_reason, str) else None
            )
        if pending_tool_blocks is not None:
            self.checkpoint.pending_tool_blocks = (
                list(pending_tool_blocks) if isinstance(pending_tool_blocks, list) else []
            )
        if completed_tool_results is not None:
            self.checkpoint.completed_tool_results = (
                dict(completed_tool_results) if isinstance(completed_tool_results, dict) else {}
            )
        self.checkpoint.force_final_pending = self.force_final_pending
        self.checkpoint.loop_guard_state = _loop_guard_state_to_dict(self.guard_state)
        self.checkpoint.pending_skill_overlay = _skill_overlay_to_dict(
            self.session.pending_skill_overlay
        )
        self.checkpoint.current_turn_allowed_tools = self.session.current_turn_allowed_tools
        self.checkpoint.permission_decisions = dict(self.session.current_turn_permission_decisions)
        self.checkpoint.background_workers = _background_workers_to_dict(
            self.session, self.checkpoint.background_workers
        )
        self.checkpoint.truncation_attempts = self.truncation_attempts
        self.checkpoint.truncation_prefix = self.truncation_prefix
        self.checkpoint.pending_truncation_feedback = self.pending_truncation_feedback
        self.checkpoint.pending_alignment = _alignment_queue_to_dicts(self.session)
        if not isinstance(self.checkpoint.extension_state, dict):
            raise ConfigError("RunCheckpoint.extension_state must be a dict")
        if self.session.active_model is not None or self.session.fallback_index:
            self.checkpoint.extension_state["linch.core"] = {
                "active_model": self.session.active_model,
                "fallback_index": self.session.fallback_index,
            }
        else:
            self.checkpoint.extension_state.pop("linch.core", None)
        if self.checkpointable_hooks:
            assert self._checkpoint_extension_state_fn is not None
            hook_state = self._checkpoint_extension_state_fn(
                self.checkpointable_hooks,
                self.session,
                self.run_id,
            )
            for key in self.checkpointable_hooks:
                self.checkpoint.extension_state.pop(key, None)
            self.checkpoint.extension_state.update(hook_state)
        if phase == "tool_batch_pending" or (
            phase == "assistant_appended"
            and self.checkpoint.assistant_message is not None
            and any(
                isinstance(block, ToolUseBlock)
                for block in self.checkpoint.assistant_message.content
            )
        ):
            self.checkpoint.tool_batch_event_after_seq = buf.last_seq if buf is not None else None
        elif phase not in ("permission_pending", "tool_executing", "tool_results_appended"):
            self.checkpoint.tool_batch_event_after_seq = None
        if has_pending_events:
            assert buf is not None
            await buf.flush_with_checkpoint(self.checkpoint, status=status)
        else:
            await store_call(
                self.session,
                "run.save_checkpoint",
                store.save_checkpoint(self.run_id, self.checkpoint, status=status),
            )
        if completed_snapshot_id is not None:
            await self.best_effort_snapshot_cleanup(
                snapshot_id=completed_snapshot_id,
            )

    async def prepare_provider_call(
        self,
        request: ProviderRequest,
        turn_index: int,
    ) -> ProviderRequest:
        if not self.exact_model_input:
            return request
        assert self.model_input_store is not None
        request.signal = self.signal
        provider_attempt = self.checkpoint.provider_attempt + 1
        # Preserve the store's original exception class.  The run-level error
        # terminal adds the stable ErrorEvent envelope; wrapping here would
        # erase useful backend/fencing types (for example LeaseLostError).
        snapshot = await store_call(
            self.session,
            "run.save",
            self.model_input_store.save(self.run_id, provider_attempt, request),
        )
        if (
            not isinstance(getattr(snapshot, "id", None), str)
            or not snapshot.id
            or getattr(snapshot, "run_id", None) != self.run_id
            or getattr(snapshot, "provider_attempt", None) != provider_attempt
        ):
            raise ConfigError("run_store returned an invalid model-input snapshot")
        self.checkpoint.provider_attempt = provider_attempt
        self.checkpoint.model_input_snapshot_id = snapshot.id
        await self.best_effort_snapshot_cleanup(keep_ids=(snapshot.id,))
        return request

    async def bind_active_primary(self, intent: ModelCallIntent) -> None:
        self.active_primary = _primary_active_binding(
            intent,
            snapshot_id=self.checkpoint.model_input_snapshot_id,
            full_history_count=self.session.session_log.history_count,
            full_history_fingerprint=self.session.session_log._history_fingerprint(),
        )
        await self.save_checkpoint("provider_pending", turn_index=intent.turn_index)

    async def load_pending_provider_request(self, signal: Any) -> ProviderRequest:
        if not self.exact_model_input or self.model_input_store is None:
            raise ConfigError("exact model-input replay is not configured")
        snapshot_id = self.checkpoint.model_input_snapshot_id
        if not snapshot_id:
            raise ConfigError("provider_pending checkpoint is missing model_input_snapshot_id")
        if self.checkpoint.provider_attempt < 1:
            raise ConfigError("provider_pending checkpoint is missing a valid provider_attempt")
        try:
            snapshot = await store_call(
                self.session, "run.load", self.model_input_store.load(snapshot_id)
            )
        except (LeaseLostError, UnsupportedWireVersionError):
            raise
        except Exception as exc:
            raise ConfigError(
                f"failed to load exact model input snapshot {snapshot_id!r}: {exc}"
            ) from exc
        if snapshot is None:
            raise ConfigError(f"exact model input snapshot {snapshot_id!r} is missing")
        if (
            snapshot.run_id != self.run_id
            or snapshot.provider_attempt != self.checkpoint.provider_attempt
            or snapshot.id != snapshot_id
        ):
            raise ConfigError(
                f"exact model input snapshot {snapshot_id!r} does not match its checkpoint"
            )
        try:
            return decode_model_input_snapshot(snapshot, signal=signal)
        except UnsupportedWireVersionError:
            raise
        except ModelInputSnapshotError as exc:
            raise ConfigError(
                f"exact model input snapshot {snapshot_id!r} cannot be decoded: {exc}"
            ) from exc
